<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Copy, Image, Maximize2, Video, X } from 'lucide-vue-next'
import type { Prompt } from '../types'
import StatusPill from './StatusPill.vue'
import type { DetailTab } from '../nodeDetails'

const props = withDefaults(defineProps<{ prompt: Prompt; tab?: DetailTab | 'all'; embedded?: boolean }>(), { tab: 'all', embedded: false })
const showOverview = computed(() => props.tab === 'all' || props.tab === 'overview')
const showEvidence = computed(() => props.tab === 'all' || props.tab === 'evidence')
const showReview = computed(() => props.tab === 'all' || props.tab === 'review')
const emit = defineEmits<{ copy: [text: string] }>()
const expanded = ref(false)
const previewOpen = ref(false)
watch(() => props.prompt.id, () => { expanded.value = false; previewOpen.value = false })

const aigcPrompt = computed(() => {
  const sections = [props.prompt.text.trim()]
  if (props.prompt.negativeText?.trim()) sections.push(`Negative Prompt:\n${props.prompt.negativeText.trim()}`)
  return sections.filter(Boolean).join('\n\n') || '等待模型生成 AIGC Prompt'
})
const delivery = computed(() => props.prompt.contract?.delivery)
const styleState = computed(() => delivery.value?.style_provenance?.resolution_status)
const styleLabels: Record<string, string> = { resolved: '画风已确定', partial: '画风部分确定', unknown: '画风未知' }
const styleLabel = computed(() => styleLabels[styleState.value || ''] || '')
const fullDelivery = computed(() => {
  const contract = props.prompt.contract
  return [contract ? `镜头合同\n目的：${contract.purpose}\n首帧：${contract.firstFrameState}\n主要动作：${contract.primaryAction}\n结束：${contract.endState}\n画风：${contract.observedStyle || '未记录'}` : '',
    delivery.value?.first_frame_intent ? `首帧意图说明\n${delivery.value.first_frame_intent}` : '',
    delivery.value?.first_frame_prompt && delivery.value?.seedance_video_prompt ?
      `${delivery.value.first_frame_prompt}\n\n${delivery.value.seedance_video_prompt}` : aigcPrompt.value,
    delivery.value?.settings?.length ? `Seedance 素材与设置建议\n${delivery.value.settings.join('\n')}` : '',
    delivery.value?.continuity_checks?.length ? `连续性检查\n${delivery.value.continuity_checks.join('\n')}` : '',
  ].filter(Boolean).join('\n\n')
})
</script>

<template>
  <div v-if="!embedded && showOverview" class="prompt-inspector-heading">
    <h3>{{ prompt.eventSummary ? '完整事件 Prompt' : 'AIGC Prompt' }}</h3>
    <StatusPill :status="prompt.status" />
  </div>
  <div class="inspector-body prompt-inspector-body">
    <div v-if="showOverview" class="prompt-inspector-meta">
      <span class="prompt-icon"><Image v-if="prompt.type === 'image' || prompt.type === 'first_frame'" :size="16" /><Video v-else :size="16" /></span>
      <div><strong>{{ prompt.platform }}</strong><small>版本 {{ prompt.version }} · {{ prompt.eventSummary ? '合并事件完整提示词' : prompt.type === 'first_frame' ? '首帧图片 Prompt' : prompt.type === 'image' ? '画面 Prompt' : 'Seedance 视频 Prompt' }}</small></div>
    </div>
    <div v-if="showEvidence" class="template-version"><span>模板</span><b>{{ prompt.templateId || 'legacy-template' }}@{{ prompt.templateVersion || prompt.version }}</b></div>
    <p v-if="styleLabel && showOverview" role="status" data-testid="prompt-style-status">{{ styleLabel }}<span v-if="styleState !== 'resolved'"> · 已知特征保留，动作与摄影结果不受影响</span></p>
    <div v-if="showOverview" class="aigc-prompt-box" :class="{ expanded }">
      <textarea :value="aigcPrompt" readonly aria-label="AIGC Prompt" />
      <div class="prompt-box-actions">
        <button type="button" @click="expanded = !expanded">{{ expanded ? '收起' : '展开' }}</button>
        <button type="button" @click="previewOpen = true"><Maximize2 :size="13" /> 全部预览</button>
      </div>
    </div>
    <button v-if="showOverview" class="full-button" @click="emit('copy', aigcPrompt)"><Copy :size="14" /> 复制 AIGC Prompt</button>
    <details v-if="prompt.contract && showEvidence" class="prompt-contract">
      <summary>镜头合同</summary>
      <dl><dt>0 秒状态</dt><dd>{{ prompt.contract.firstFrameState }}</dd><dt>主体动作</dt><dd>{{ prompt.contract.primaryAction }}</dd><dt>结束状态</dt><dd>{{ prompt.contract.endState }}</dd><dt>连续性锚点</dt><dd>{{ prompt.contract.continuityAnchors.join('、') || '无' }}</dd></dl>
      <dl><dt>整体画风</dt><dd>{{ prompt.contract.observedStyle || '历史任务未记录' }}</dd><dt>生成建议（非原片实测）</dt><dd v-for="(value, key) in prompt.contract.generationSuggestions" :key="key">{{ value }}</dd></dl>
    </details>
    <details v-if="delivery?.schema_version && showReview" class="prompt-contract">
      <summary>首帧意图、设置与连续性检查</summary>
      <p>{{ delivery.first_frame_intent }}</p>
      <h4>Seedance 素材与设置建议</h4><ul><li v-for="item in delivery.settings" :key="item">{{ item }}</li></ul>
      <h4>连续性检查</h4><ul><li v-for="item in delivery.continuity_checks" :key="item">{{ item }}</li></ul>
      <button class="outline-button" type="button" @click="emit('copy', fullDelivery)"><Copy :size="14" /> 复制提示词与交付说明</button>
    </details>
    <div v-if="prompt.warnings?.length && showReview" class="analysis-warning"><strong>模板警告</strong><span v-for="warning in prompt.warnings" :key="warning">{{ warning }}</span></div>
    <details v-if="prompt.sequenceContract && showEvidence" class="prompt-contract" data-testid="sequence-prompt-contract"><summary>生成片段合同</summary><dl><dt>片段目的</dt><dd>{{ prompt.sequenceContract.purpose }}</dd><dt>首帧状态</dt><dd>{{ prompt.sequenceContract.first_frame_state }}</dd><dt>结束状态</dt><dd>{{ prompt.sequenceContract.end_state }}</dd><dt>建议生成时长</dt><dd>{{ prompt.sequenceContract.generation_duration_seconds || '待设定' }} 秒</dd><dt>连续性锚点</dt><dd>{{ Array.isArray(prompt.sequenceContract.continuity_anchors) ? prompt.sequenceContract.continuity_anchors.join('、') : '未记录' }}</dd></dl><p>{{ prompt.sequenceContract.editorial_note }}</p></details>
    <p v-if="tab === 'review' && !prompt.warnings?.length && !delivery?.schema_version" class="action-timing-note">此提示词未记录额外交付警告或检查项。</p>
  </div>

  <Teleport to="body">
    <div v-if="previewOpen" class="prompt-modal-backdrop" data-canvas-modal @click.self="previewOpen = false" @keydown.esc.stop.prevent="previewOpen = false">
      <section class="prompt-modal" data-canvas-modal role="dialog" aria-modal="true" aria-label="AIGC Prompt 全部预览">
        <header><div><span class="eyebrow">FULL PREVIEW</span><h2>AIGC Prompt</h2></div><button class="icon-button" @click="previewOpen = false"><X :size="18" /></button></header>
        <textarea :value="aigcPrompt" readonly />
        <footer><span>{{ aigcPrompt.length }} 字符</span><button class="primary-button" @click="emit('copy', aigcPrompt)"><Copy :size="14" /> 复制全部</button></footer>
      </section>
    </div>
  </Teleport>
</template>
