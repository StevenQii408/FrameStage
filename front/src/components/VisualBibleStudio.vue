<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, RefreshCw, Save, X } from 'lucide-vue-next'
import {
  confirmVisualBible, draftVisualBible, getVisualBible, getVisualBibleCatalog,
  getVisualBibleIndexHealth, saveVisualBible,
  type VisualBibleCatalog, type VisualBibleData, type VisualBibleMatch,
} from '../api/jobs'
import { apiUrl } from '../api/client'

const props = defineProps<{ jobId: string; inputType: 'video' | 'script'; shotIds: string[]; sceneIds: string[]; selectedShotId?: string }>()
const emit = defineEmits<{ close: []; applied: [jobId: string] }>()
const catalog = ref<VisualBibleCatalog | null>(null)
const bible = ref<VisualBibleData | null>(null)
const matches = ref<VisualBibleMatch[]>([])
const unresolved = ref<Array<{ shot_id: string; category: string; description: string; image_missing?: boolean; candidates: Array<{ term_id: string; ranks: Record<string, number> }> }>>([])
const indexHealth = ref<{ ready: boolean; reason: string; manifest: Record<string, unknown> | null } | null>(null)
const selectedShot = ref(props.selectedShotId || props.shotIds[0] || '')
const selectedScene = ref(props.sceneIds[0] || '')
const effectText = ref('')
const busy = ref(false)
const dirty = ref(false)
const error = ref('')
const categoryNames: Record<string, string> = {
  medium_rendering: '媒介与渲染', form_language: '造型语言',
  color_tone: '色彩与调性', lighting_shading: '光影组织',
  texture_material: '表面纹理', motion_signature: '动态语言',
}
const termMap = computed(() => Object.fromEntries((catalog.value?.terms || []).map(term => [term.id, term])))
const name = (id: string) => termMap.value[id]?.name_cn || id
const termsIn = (category: string) => catalog.value?.terms.filter(term => term.category === category) || []
const matchesFor = (id: string) => matches.value.filter(item => item.term_id === id)
const recommendationNames: Record<string, string> = { candidate: '召回候选 · 未核验', supported: '视觉支持 · 创作建议', partial: '部分支持 · 创作建议', rejected: '存在反证 · 不推荐' }
const orderedRecipes = computed(() => {
  const preferred = bible.value?.recommended_recipes || []
  return [...(catalog.value?.recipes || [])].sort((a, b) => {
    const aRank = preferred.indexOf(a.id), bRank = preferred.indexOf(b.id)
    return (aRank < 0 ? 999 : aRank) - (bRank < 0 ? 999 : bRank)
  })
})
const chosenRecipe = computed(() => catalog.value?.recipes.find(item => item.id === bible.value?.selected_recipe_id))
const proposedForbidden = computed(() => {
  const selected = new Set([...(bible.value?.global_core_terms || []),
    ...(catalog.value?.recipes.find(item => item.id === bible.value?.selected_recipe_id)?.term_ids || [])])
  return [...new Set([...selected].flatMap(id => termMap.value[id]?.conflicts_with || []))]
    .filter(id => !selected.has(id))
})
function toggleForbidden(id: string) {
  if (!bible.value) return
  const current = bible.value.forbidden_drift
  bible.value.forbidden_drift = current.includes(id) ? current.filter(item => item !== id) : [...current, id]
  changed()
}
const scopeNames: Record<string, string> = { GLOBAL: '全局', SCENE: '场次', SHOT: '镜头', UNCERTAIN: '待判断' }

function blank(): VisualBibleData {
  return {
    schema_version: 'project-visual-bible.v2', status: 'draft',
    mode: props.inputType === 'script' ? 'creative' : 'observed',
    version: 1, ontology_version: catalog.value?.version || 'style-ontology.v2',
    index_manifest: indexHealth.value?.manifest || {},
    global_core_terms: [], global_secondary_terms: [], scene_overrides: {},
    shot_overrides: {}, local_effects: {}, forbidden_drift: [],
    recommended_recipes: catalog.value?.recipes.map(item => item.id) || [],
    selected_recipe_id: null, selected_recipe_term_ids: [], coverage_profiles: {}, unresolved_features: [],
    rendered_global_prompt: '', model_provenance: {}, bible_hash: '',
  }
}
function changed() {
  if (bible.value?.status === 'confirmed') bible.value.status = 'draft'
  dirty.value = true
  error.value = ''
}
function toggleGlobal(id: string, category: string) {
  if (!bible.value) return
  const current = bible.value.global_core_terms
  if (current.includes(id)) bible.value.global_core_terms = current.filter(item => item !== id)
  else {
    bible.value.global_core_terms = [...current.filter(item => termMap.value[item]?.category !== category), id]
    if (bible.value.global_core_terms.length > 6) bible.value.global_core_terms.shift()
  }
  bible.value.selected_recipe_id = null
  changed()
}
function chooseRecipe(id: string) {
  if (!bible.value) return
  if (bible.value.selected_recipe_id === id) {
    bible.value.selected_recipe_id = null
    bible.value.selected_recipe_term_ids = []
  } else {
    const recipe = catalog.value?.recipes.find(item => item.id === id)
    if (!recipe) return
    bible.value.selected_recipe_id = id
    bible.value.selected_recipe_term_ids = recipe.term_ids || []
    const seen = new Set<string>()
    bible.value.global_core_terms = (recipe.term_ids || []).filter(termId => {
      const category = termMap.value[termId]?.category
      if (!category || seen.has(category)) return false
      seen.add(category)
      return true
    })
    bible.value.global_secondary_terms = []
  }
  changed()
}
function toggleOverride(scope: 'scene' | 'shot', id: string) {
  if (!bible.value) return
  const key = scope === 'scene' ? selectedScene.value : selectedShot.value
  if (!key) return
  const map = scope === 'scene' ? bible.value.scene_overrides : bible.value.shot_overrides
  const current = map[key] || []
  map[key] = current.includes(id) ? current.filter(item => item !== id) : [...current, id]
  changed()
}
function addEffect() {
  if (!bible.value || !selectedShot.value) return
  const value = effectText.value.trim()
  if (!value || value.length > 150) { error.value = '局部效果需为 1–150 字'; return }
  bible.value.local_effects[selectedShot.value] = [...(bible.value.local_effects[selectedShot.value] || []), value]
  effectText.value = ''
  changed()
}
function removeEffect(index: number) {
  if (!bible.value || !selectedShot.value) return
  bible.value.local_effects[selectedShot.value]?.splice(index, 1)
  changed()
}
async function load() {
  busy.value = true
  error.value = ''
  try {
    const [found, dictionary, health] = await Promise.all([
      getVisualBible(props.jobId), getVisualBibleCatalog(), getVisualBibleIndexHealth(),
    ])
    bible.value = found.bible
    matches.value = found.retrieval?.matches || []
    unresolved.value = found.retrieval?.unresolved || []
    catalog.value = dictionary
    indexHealth.value = health
    dirty.value = false
  } catch (cause) { error.value = cause instanceof Error ? cause.message : 'Visual Bible 读取失败' }
  finally { busy.value = false }
}
async function redraft() {
  busy.value = true
  error.value = ''
  try {
    const found = await draftVisualBible(props.jobId)
    bible.value = found.bible
    matches.value = found.retrieval?.matches || []
    unresolved.value = found.retrieval?.unresolved || []
    dirty.value = false
    if (!found.bible) error.value = found.retrieval?.errors?.map(item => item.error).join('；') || '自动草案未生成，请检查向量索引或人工创建'
    if (found.bible?.status === 'confirmed') emit('applied', props.jobId)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '重新分析失败' }
  finally { busy.value = false }
}
async function save() {
  if (!bible.value) return false
  busy.value = true
  error.value = ''
  try {
    bible.value = await saveVisualBible(props.jobId, bible.value)
    dirty.value = false
    return true
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败'; return false }
  finally { busy.value = false }
}
async function confirm() {
  if (dirty.value && !(await save())) return
  busy.value = true
  error.value = ''
  try {
    bible.value = await confirmVisualBible(props.jobId)
    emit('applied', props.jobId)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '确认失败' }
  finally { busy.value = false }
}
watch(() => props.jobId, load)
watch(() => props.selectedShotId, value => { if (value) selectedShot.value = value })
onMounted(load)
</script>

<template>
  <div class="bible-backdrop" @click.self="emit('close')">
    <section class="bible-panel" role="dialog" aria-modal="true" aria-label="Visual Bible 画风编辑器">
      <header class="bible-head">
        <div><small>PROJECT VISUAL BIBLE / V2</small><h2>项目视觉规范</h2><p>视频画风按帧证据自动应用；此处可选调整创作配方，场次与镜头变化分层保存。</p></div>
        <button type="button" aria-label="关闭画风面板" @click="emit('close')"><X :size="18" /></button>
      </header>
      <div class="bible-status">
        <span>{{ bible?.status === 'confirmed' ? bible.model_provenance?.application_mode === 'auto-evidence' ? '系统已识别并应用' : '已确认并应用' : bible?.global_core_terms.length ? '已识别部分特征 · 可调整应用' : '证据不足 · 可人工设定' }} · 版本 {{ bible?.version || '—' }}</span>
        <span>索引 {{ indexHealth?.ready ? '就绪' : '未就绪' }} · {{ catalog?.version || '—' }}</span>
        <span>文本 text-embedding-v4 · 图像 qwen3-vl-embedding</span>
      </div>
      <p v-if="!indexHealth?.ready && inputType === 'video'" class="bible-warning">{{ indexHealth?.reason }}。仍可人工创建草案。</p>
      <p v-if="error" class="bible-error" role="alert">{{ error }}</p>
      <p v-if="busy" role="status">正在处理视觉规范…</p>
      <main v-if="catalog" class="bible-content">
        <template v-if="bible">
          <section><h3>原片已识别特征 / 当前画风草案</h3><p class="bible-preview">{{ dirty ? '保存后生成确定性预览；当前 Prompt 保持不变。' : bible.rendered_global_prompt }}</p>
            <p class="bible-muted">已证实多少就展示多少，不强制补齐维度。草案尚未确认时，镜头 Prompt 保持不变。</p>
            <p v-if="bible.unresolved_features.length" class="bible-muted">{{ bible.unresolved_features.join('；') }}</p>
          </section>
          <section><h3>创作配方推荐（不是原片来源识别）</h3>
            <p class="bible-muted">向量只召回候选。确认配方会引入完整创作特征，不表示原片已经具备全部特征。</p>
            <div v-for="candidate in bible.recipe_recommendations || []" :key="candidate.recipe_id" class="bible-match">
              <b>{{ catalog.recipes.find(item => item.id === candidate.recipe_id)?.name }}</b><span>{{ recommendationNames[candidate.status] }}</span>
              <small>{{ candidate.reason }}</small><small>部分镜头支持：{{ candidate.supported_term_ids.map(name).join('、') || '暂无' }}；未观察/需创作确认：{{ candidate.missing_term_ids.map(name).join('、') || '无' }}</small>
              <small v-if="candidate.optional_anchors?.length">可选名称锚点：{{ candidate.optional_anchors.join('、') }}</small>
              <a v-for="frame in candidate.evidence" :key="frame.frame_id" :href="apiUrl(`/jobs/${encodeURIComponent(jobId)}/frames/${encodeURIComponent(frame.frame_id)}`)" target="_blank" rel="noopener noreferrer">核验帧 @ {{ frame.timestamp.toFixed(2) }}s</a>
            </div>
            <div class="bible-options"><button v-for="recipe in orderedRecipes" :key="recipe.id" type="button" :class="{ selected: bible.selected_recipe_id === recipe.id }" @click="chooseRecipe(recipe.id)">{{ bible.recommended_recipes.includes(recipe.id) ? '推荐 · ' : '' }}{{ recipe.name }}</button></div>
            <p v-if="chosenRecipe" class="bible-muted">{{ chosenRecipe.global_prompt }}<br />配方术语：{{ chosenRecipe.term_ids?.map(name).join('、') }}<br />{{ chosenRecipe.optional_anchors?.join('、') }}。名称不用于来源判断，也不会自动写入观察事实。</p>
          </section>
          <section><h3>六类全局核心特征</h3><p class="bible-muted">每类最多一项。人物服装、道具、地点及单镜构图不进入这里。</p>
            <div v-for="category in catalog.categories" :key="category" class="bible-category"><strong>{{ categoryNames[category] }}</strong><div class="bible-options"><button v-for="term in termsIn(category)" :key="term.id" type="button" :class="{ selected: bible.global_core_terms.includes(term.id) }" :title="term.definition" @click="toggleGlobal(term.id, category)">{{ term.name_cn }}</button></div></div>
          </section>
          <section v-if="proposedForbidden.length || bible.forbidden_drift.length"><h3>风格漂移禁用项</h3><p class="bible-muted">根据所选全局术语和配方的冲突关系提出，只有你点选并确认后才写入 Prompt。</p><div class="bible-options"><button v-for="id in [...new Set([...proposedForbidden, ...bible.forbidden_drift])]" :key="id" type="button" :class="{ selected: bible.forbidden_drift.includes(id) }" @click="toggleForbidden(id)">{{ name(id) }}</button></div></section>
          <section v-if="inputType === 'video'"><h3>候选与帧证据</h3><p class="bible-muted">向量排名只负责召回；点击帧可核对可见依据。</p>
            <div v-for="match in matches" :key="match.term_id + match.shot_ids[0]" class="bible-match"><b>{{ name(match.term_id) }}</b><span>{{ scopeNames[match.scope] || match.scope }} · {{ match.shot_ids[0] }}</span><small>文本排名 {{ match.ranks.text || '—' }} / 图像排名 {{ match.ranks.image || '—' }} · {{ match.reason }}</small><a v-for="evidence in match.evidence" :key="evidence.frame_id" :href="apiUrl(`/jobs/${encodeURIComponent(jobId)}/frames/${encodeURIComponent(evidence.frame_id)}`)" target="_blank" rel="noopener noreferrer">帧 {{ evidence.frame_id }} @ {{ evidence.timestamp.toFixed(2) }}s</a></div>
            <p v-if="!matches.length" class="bible-muted">暂无可确认的双通道匹配。请检查索引、证据或人工选择。</p>
            <details v-if="unresolved.length"><summary>{{ unresolved.length }} 项待判断候选</summary><div v-for="item in unresolved.slice(0, 30)" :key="item.shot_id + item.category" class="bible-match"><b>{{ item.shot_id }} · {{ categoryNames[item.category] }}</b><span>{{ item.description }}</span><small v-if="item.image_missing">图像通道缺失，不能当作双通道确认</small><small>{{ item.candidates.slice(0, 5).map(row => name(row.term_id) + ' [文' + (row.ranks.text || '—') + ' / 图' + (row.ranks.image || '—') + ']').join('、') }}</small></div></details>
          </section>
          <section v-if="sceneIds.length"><h3>场次覆盖</h3><select v-model="selectedScene" aria-label="选择场次"><option v-for="id in sceneIds" :key="id" :value="id">{{ id }}</option></select><div class="bible-options"><button v-for="term in catalog.terms" :key="term.id" type="button" :class="{ selected: bible.scene_overrides[selectedScene]?.includes(term.id) }" @click="toggleOverride('scene', term.id)">{{ term.name_cn }}</button></div></section>
          <section v-if="shotIds.length"><h3>镜头覆盖与局部效果</h3><select v-model="selectedShot" aria-label="选择镜头"><option v-for="(id, index) in shotIds" :key="id" :value="id">SH {{ String(index + 1).padStart(2, '0') }}</option></select><div class="bible-options"><button v-for="term in catalog.terms" :key="term.id" type="button" :class="{ selected: bible.shot_overrides[selectedShot]?.includes(term.id) }" @click="toggleOverride('shot', term.id)">{{ term.name_cn }}</button></div><div class="bible-effect"><input v-model="effectText" maxlength="150" placeholder="仅本镜可见的局部效果" @keyup.enter="addEffect" /><button type="button" @click="addEffect">追加</button></div><div class="bible-options"><button v-for="(effect, index) in bible.local_effects[selectedShot] || []" :key="index" type="button" @click="removeEffect(index)">{{ effect }} ×</button></div></section>
        </template>
        <section v-else><h3>尚无 Visual Bible</h3><p class="bible-muted">视频任务需要构建向量索引；若云端不可用，逐镜观察与现有 Prompt 不会丢失。</p><button type="button" @click="bible = blank(); changed()">人工创建草案</button></section>
      </main>
      <footer class="bible-actions"><button type="button" :disabled="busy" @click="redraft"><RefreshCw :size="15" />重新分析</button><button type="button" :disabled="busy || !dirty || !bible" @click="save"><Save :size="15" />保存草案</button><button type="button" :disabled="busy || !bible || (bible.status === 'confirmed' && !dirty)" @click="confirm"><Check :size="15" />确认并应用</button></footer>
    </section>
  </div>
</template>

<style scoped>
.bible-backdrop{position:fixed;inset:0;z-index:1000;display:grid;place-items:center;background:rgba(4,8,16,.76);backdrop-filter:blur(10px)}
.bible-panel{width:min(1040px,94vw);max-height:92vh;display:flex;flex-direction:column;overflow:hidden;border:1px solid #3c5264;border-radius:18px;background:#171c25;color:#edf5fa;box-shadow:0 24px 80px #0009}
.bible-head{display:flex;justify-content:space-between;align-items:flex-start;padding:24px 28px 16px;border-bottom:1px solid #33414f}
.bible-head small{color:#79b4d2;letter-spacing:.14em}.bible-head h2{margin:7px 0;font-size:23px}.bible-head p,.bible-muted{color:#9eacbb;font-size:13px}.bible-head button{border:0;background:none;color:inherit;cursor:pointer}
.bible-status{display:flex;gap:16px;flex-wrap:wrap;padding:10px 28px;font-size:12px;color:#94b6c5;border-bottom:1px solid #293a49}
.bible-warning,.bible-error{margin:10px 28px;padding:10px;border-radius:8px;background:#4a3625;color:#ffd9a3}.bible-error{background:#502e35;color:#ffc5c5}
.bible-content{overflow:auto;padding:4px 28px 24px}.bible-content section{padding:16px 0;border-bottom:1px solid #2c3947}.bible-content h3{font-size:15px;margin:0 0 10px}.bible-preview{line-height:1.8;color:#e9d6b7}
.bible-category{margin:14px 0}.bible-category strong{display:block;margin-bottom:7px;font-size:13px;color:#b8d5e5}.bible-options{display:flex;gap:6px;flex-wrap:wrap}.bible-options button,.bible-content select,.bible-content input,.bible-effect button,.bible-content section>button{border:1px solid #405365;border-radius:8px;background:#242e3a;color:#d7e7ef;padding:7px 9px;cursor:pointer;font-size:12px}.bible-options button.selected{border-color:#69d0d9;background:#174652;color:#e4ffff}
.bible-match{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:7px 0;border-bottom:1px solid #283541;font-size:12px}.bible-match span,.bible-match small{color:#91a9b9}.bible-match a{color:#74cae7}.bible-effect{display:flex;gap:8px;margin:12px 0}.bible-effect input{flex:1}
.bible-actions{display:flex;justify-content:flex-end;gap:9px;padding:15px 28px;border-top:1px solid #33414f}.bible-actions button{display:flex;align-items:center;gap:6px;border:1px solid #4c6572;border-radius:9px;background:#263844;color:#e6f8ff;padding:9px 12px;cursor:pointer}.bible-actions button:last-child{background:#187b85}.bible-actions button:disabled{opacity:.45;cursor:default}
@media(max-width:650px){.bible-status{gap:5px}.bible-head{padding:18px}.bible-content{padding:4px 18px 18px}}
</style>
