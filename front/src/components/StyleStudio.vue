<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { X, RefreshCw, Check, Save } from 'lucide-vue-next'
import { confirmJobStyle, getJobStyle, getStyleCatalog, regenerateJobStyle, saveJobStyle, type StyleCatalog, type StyleObservationData, type StyleProfileData } from '../api/jobs'
import { apiUrl } from '../api/client'

const props = defineProps<{ jobId: string; inputType: 'video' | 'script'; shotIds: string[]; selectedShotId?: string }>()
const emit = defineEmits<{ close: []; applied: [jobId: string] }>()
const catalog = ref<StyleCatalog | null>(null)
const observation = ref<StyleObservationData | null>(null)
const profile = ref<StyleProfileData | null>(null)
const busy = ref(false)
const error = ref('')
const dirty = ref(false)
const currentShot = ref(props.selectedShotId || props.shotIds[0] || '')
const dimensions = [
  ['medium', '媒介与维度'], ['style_family', '风格流派'], ['line_language', '造型与线条'],
  ['character_design', '角色设计'], ['palette', '色彩语法'], ['shading', '光影与阴影'],
  ['material', '材质与表面'], ['composition', '构图与密度'], ['graphic_motif', '图形母题'],
  ['motion_language', '动态画风'],
] as const
const termMap = computed(() => Object.fromEntries((catalog.value?.terms || []).map((term) => [term.id, term])))
const selectedByCategory = (category: string) => profile.value?.global_terms.filter((id) => termMap.value[id]?.category === category) || []
const termsIn = (category: string) => catalog.value?.terms.filter((term) => term.category === category) || []
const label = (id: string) => termMap.value[id]?.zh_label || id
function markChanged() {
  if (profile.value && props.inputType === 'video') profile.value.mode = 'hybrid'
  dirty.value = true; error.value = ''
}
function toggleTerm(id: string, category: string) {
  if (!profile.value) return
  const selected = profile.value.global_terms
  if (selected.includes(id)) profile.value.global_terms = selected.filter((item) => item !== id)
  else {
    if (selectedByCategory(category).length >= 2) { error.value = '每个维度最多保留两个术语'; return }
    profile.value.global_terms = [...selected, id]
  }
  markChanged()
}
function toggleAnchor(id: string) {
  if (!profile.value) return
  const selected = profile.value.optional_anchors
  profile.value.optional_anchors = selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id].slice(-2)
  markChanged()
}
function toggleNegative(id: string) {
  if (!profile.value) return
  const selected = profile.value.negative_constraints
  profile.value.negative_constraints = selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id]
  markChanged()
}
function toggleEffect(id: string) {
  if (!profile.value || !currentShot.value) return
  const selected = profile.value.per_shot_effect_overrides[currentShot.value] || []
  if (!selected.includes(id) && selected.length >= 2) { error.value = '每镜最多两个局部效果'; return }
  profile.value.per_shot_effect_overrides = {
    ...profile.value.per_shot_effect_overrides,
    [currentShot.value]: selected.includes(id) ? selected.filter((item) => item !== id) : [...selected, id],
  }
  markChanged()
}
async function load() {
  busy.value = true
  error.value = ''
  try {
    const [found, dictionary] = await Promise.all([getJobStyle(props.jobId), getStyleCatalog()])
    profile.value = found.profile
    observation.value = found.observation
    catalog.value = dictionary
    dirty.value = false
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '画风读取失败' }
  finally { busy.value = false }
}
async function draft() {
  busy.value = true; error.value = ''
  try {
    const result = await regenerateJobStyle(props.jobId)
    profile.value = result.profile
    observation.value = result.observation
    dirty.value = false
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '草案生成失败' }
  finally { busy.value = false }
}
function startManual() {
  const selected = observation.value ? Object.values(observation.value.dimensions).flat().map((item) => item.term_id) : []
  profile.value = {
    schema_version: "style-profile.v1", status: "draft", mode: props.inputType === "video" ? "hybrid" : "creative",
    version: 1, global_terms: selected.filter((id) => !id.startsWith("visible_effect.") && !id.startsWith("negative_constraint.")),
    optional_anchors: [], expanded_anchor_traits: [], include_anchor_names: false,
    negative_constraints: ["negative_constraint.no-readable-text", "negative_constraint.no-logo"],
    per_shot_effect_overrides: {}, rendered_style_prompt: "", profile_hash: "",
  }
  markChanged()
}
async function save() {
  if (!profile.value) return false
  busy.value = true; error.value = ''
  try {
    profile.value = await saveJobStyle(props.jobId, profile.value)
    dirty.value = false
    return true
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '画风保存失败'; return false }
  finally { busy.value = false }
}
async function confirm() {
  if (dirty.value && !(await save())) return
  busy.value = true; error.value = ''
  try {
    profile.value = await confirmJobStyle(props.jobId)
    emit('applied', props.jobId)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '应用画风失败' }
  finally { busy.value = false }
}
watch(() => props.jobId, () => { currentShot.value = props.selectedShotId || props.shotIds[0] || ""; void load() })
watch(() => props.selectedShotId, (id) => { if (id) currentShot.value = id })
onMounted(load)
</script>

<template>
  <div class="style-studio-backdrop" @click.self="emit('close')">
    <section class="style-studio" role="dialog" aria-modal="true" aria-label="全局画风草案">
      <header class="style-studio-head">
        <div><small>STYLE PROFILE / V1</small><h2>全局画风草案</h2><p>先核对视觉证据，再确认并应用到全部镜头</p></div>
        <button type="button" aria-label="关闭画风面板" @click="emit('close')"><X :size="19" /></button>
      </header>
      <div v-if="error" class="style-studio-error" role="alert">{{ error }}</div>
      <div v-if="busy" class="style-studio-loading" role="status">正在处理画风配置…</div>
      <template v-if="catalog">
        <div class="style-studio-summary">
          <span>{{ profile?.status === 'confirmed' ? '已确认' : profile ? '待确认草案' : '尚无草案' }}</span>
          <span>版本 {{ profile?.version || '—' }} · {{ profile?.mode || (inputType === 'video' ? 'observed' : 'creative') }}</span>
          <span>词典 {{ catalog.version }}</span>
        </div>
        <template v-if="profile">
          <div class="style-studio-body">
            <section><h3>高密度中文画风句</h3><p class="style-studio-preview">{{ dirty ? '保存草案后更新预览。' : profile.rendered_style_prompt }}</p><small>80–140 字；确认前不会覆盖现有 Prompt。</small></section>
            <section v-if="observation && inputType === 'video'"><h3>观察依据</h3><div class="style-studio-evidence" v-for="(claims, dimension) in observation.dimensions" :key="dimension"><strong>{{ dimensions.find((item) => item[0] === dimension)?.[1] || dimension }}</strong><span v-for="claim in claims" :key="claim.term_id">{{ label(claim.term_id) }} <small v-if="!claim.evidence.length">无帧证据</small><a v-for="item in claim.evidence" :key="item.frame_id" :href="apiUrl(`/jobs/${encodeURIComponent(jobId)}/frames/${encodeURIComponent(item.frame_id)}`)" target="_blank" rel="noopener noreferrer">{{ item.frame_id }} @ {{ item.timestamp.toFixed(2) }}s</a></span></div><p v-if="observation.uncertainties.length" class="style-studio-note">待判断：{{ observation.uncertainties.join('；') }}</p></section>
            <section><h3>全局视觉维度</h3><p class="style-studio-note">每维最多两项。全局特征对所有镜头保持一致。</p><div v-for="[category, title] in dimensions" :key="category" class="style-studio-dimension"><strong>{{ title }}</strong><div class="style-studio-options"><button v-for="term in termsIn(category)" :key="term.id" type="button" :class="{ selected: profile.global_terms.includes(term.id) }" :title="term.definition" @click="toggleTerm(term.id, category)">{{ term.zh_label }}</button></div></div></section>
            <section><h3>可选风格锚点</h3><p class="style-studio-note">名称只是参考；输出始终保留独立的视觉特征。</p><div class="style-studio-options"><button v-for="(anchor, id) in catalog.anchors" :key="id" type="button" :class="{ selected: profile.optional_anchors.includes(String(id)) }" @click="toggleAnchor(String(id))">{{ anchor.name }} · {{ anchor.type }}</button></div><label class="style-studio-check"><input v-model="profile.include_anchor_names" type="checkbox" @change="markChanged" />在 Prompt 中显示名称</label></section>
            <section><h3>禁用项</h3><div class="style-studio-options"><button v-for="term in termsIn('negative_constraint')" :key="term.id" type="button" :class="{ selected: profile.negative_constraints.includes(term.id) }" @click="toggleNegative(term.id)">{{ term.zh_label }}</button></div></section>
            <section v-if="shotIds.length"><h3>单镜局部特效</h3><p class="style-studio-note">只追加到所选镜头的视频 Prompt，不改变全局画风句。</p><select v-model="currentShot" aria-label="选择镜头"><option v-for="(shot, index) in shotIds" :key="shot" :value="shot">SH {{ String(index + 1).padStart(2, '0') }}</option></select><div class="style-studio-options"><button v-for="term in termsIn('visible_effect')" :key="term.id" type="button" :class="{ selected: profile.per_shot_effect_overrides[currentShot]?.includes(term.id) }" @click="toggleEffect(term.id)">{{ term.zh_label }}</button></div></section>
          </div>
          <footer class="style-studio-actions"><button type="button" :disabled="busy" @click="draft"><RefreshCw :size="15" />重新分析</button><button type="button" :disabled="busy || !dirty" @click="save"><Save :size="15" />保存草案</button><button class="primary" type="button" :disabled="busy || (profile.status === 'confirmed' && !dirty)" @click="confirm"><Check :size="15" />确认并应用到全部镜头</button></footer>
        </template>
        <div v-else class="style-studio-empty"><p>当前任务尚无可直接应用的画风草案。观察证据仍保留；可人工补足至少八个视觉维度，或重新分析。</p><p v-if="observation?.uncertainties.length" class="style-studio-note">{{ observation.uncertainties.join("；") }}</p><div v-if="observation" class="style-studio-recovered"><div v-for="(claims, dimension) in observation.dimensions" :key="dimension"><strong>{{ dimension }}</strong><span v-for="claim in claims" :key="claim.term_id">{{ label(claim.term_id) }} <a v-for="item in claim.evidence" :key="item.frame_id" :href="apiUrl(`/jobs/${encodeURIComponent(jobId)}/frames/${encodeURIComponent(item.frame_id)}`)" target="_blank" rel="noopener noreferrer">{{ item.frame_id }} @ {{ item.timestamp.toFixed(2) }}s</a></span></div></div><button type="button" :disabled="busy" @click="startManual">根据观察人工补全</button> <button type="button" :disabled="busy" @click="draft">重新生成项目草案</button></div>
      </template>
    </section>
  </div>
</template>
