<script setup lang="ts">
import { computed } from 'vue'
import { Copy } from 'lucide-vue-next'
import StatusPill from './StatusPill.vue'
import { sequenceMembers, sequenceStatus } from '../generationSequences'
import type { GenerationSequence, GenerationSequenceTimeline, NarrativeEvent, Shot } from '../types'
import type { DetailTab } from '../nodeDetails'
const props = withDefaults(defineProps<{ sequence: GenerationSequence; shots: Shot[]; event?: NarrativeEvent; preview?: boolean; metadata?: Record<string, unknown>; tab?: DetailTab | 'all' }>(), { tab: 'all' })
const showOverview = computed(() => props.tab === 'all' || props.tab === 'overview')
const showEvidence = computed(() => props.tab === 'all' || props.tab === 'evidence')
const showReview = computed(() => props.tab === 'all' || props.tab === 'review')
const emit = defineEmits<{ copy: [text: string]; sourceShot: [id: string]; openPrompt: [id: string] }>()
const members = computed(() => sequenceMembers(props.sequence, props.shots))
const cover = computed(() => members.value.flatMap(shot => shot.frames).find(frame => frame.id === props.sequence.cover_frame_id) || members.value[0]?.frames[0])
const prompts = computed(() => [props.sequence.first_frame_prompt, props.sequence.seedance_prompt].filter(prompt => !!prompt))
const decisions = computed(() => {
  const rows = props.metadata?.merge_decisions
  return Array.isArray(rows) ? rows.filter(row => row && (props.sequence.source_shot_ids.includes(row.left_shot_id) || props.sequence.source_shot_ids.includes(row.right_shot_id))) : []
})
const relationIncomplete = computed(() => ['partial', 'failed'].includes(String(props.metadata?.relation_analysis_status || '')))
function seconds(value: number) { return Number(value.toFixed(3)).toString() }
function stageSourceIds(item: GenerationSequenceTimeline): string[] {
  const valid = new Set(members.value.map(shot => shot.id))
  return [...new Set(item.source_shot_ids?.length ? item.source_shot_ids : [item.source_shot_id])]
    .filter(id => valid.has(id))
}
</script>

<template>
  <div class="inspector-body generation-sequence-inspector" data-testid="generation-sequence-inspector">
    <div v-if="cover && showOverview" class="inspector-image" :style="{ aspectRatio: cover.width && cover.height ? `${cover.width} / ${cover.height}` : '16 / 9' }"><img :src="cover.url" alt="片段来源参考帧" /><span class="image-time">来源参考帧 · {{ seconds(cover.timestamp) }}s</span></div>
    <StatusPill v-if="showOverview" :status="sequenceStatus(sequence.status)" />
    <p v-if="showOverview" class="inspector-summary">{{ sequence.main_action || event?.goal || sequence.title }}</p>
    <p v-if="preview && showOverview" class="sequence-note">由已保存的来源观察整理的生成建议，可查看与复制。</p>
    <p v-if="relationIncomplete && showReview" class="analysis-warning" data-testid="merge-incomplete">跨镜关系部分未完成：有效合并已保留，其余边界需复核，不代表这些镜头必须独立。</p>
    <div v-if="showOverview" class="metric-grid"><div><span>来源镜头</span><b>{{ sequence.source_shot_ids.length }} 个</b></div><div><span>原片总时长</span><b>{{ seconds(sequence.source_duration_seconds) }}s</b></div><div><span>原片范围</span><b>{{ seconds(sequence.source_start_time) }}–{{ seconds(sequence.source_end_time) }}s</b></div><div><span>建议生成时长</span><b>{{ sequence.generation_duration_seconds ? `${seconds(sequence.generation_duration_seconds)}s` : '待设定' }}</b></div></div>
    <p v-if="showOverview" class="sequence-note">生成时间是编排建议，原片时间是来源范围。{{ sequence.generation_mode === 'continuous' ? '多个来源角度整合为连续事件；原片切点仅用于追溯，不要求生成时逐次切镜。' : '选定的内部切镜在同一连贯事件内按时序呈现。' }}</p>
    <div v-if="event && showOverview" class="field-group"><label>叙事目的</label><div class="field-value">{{ event.goal }}</div><label>事件状态</label><div class="field-value">{{ event.initial_state }} → {{ event.final_state }}</div></div>
    <section v-if="showOverview || showEvidence" class="inspector-section" data-testid="sequence-timeline">
      <div class="section-title">事件动作阶段 <span>{{ sequence.timeline.length }} STAGES</span></div>
      <ol class="sequence-timeline">
        <li v-for="(item, index) in sequence.timeline" :key="`${item.source_shot_id}-${index}`">
          <h4 v-if="item.semantic_phase" class="sequence-phase-name" data-testid="semantic-phase">阶段 {{ index + 1 }} · {{ item.semantic_phase }}</h4>
          <b>{{ item.transition === 'cut' ? '切镜' : item.transition === 'continuation' ? '延续' : '起始' }} · 生成 {{ seconds(item.generation_start_seconds) }}–{{ seconds(item.generation_end_seconds) }}s</b>
          <small v-if="showEvidence">来源 {{ seconds(item.source_start_time) }}–{{ seconds(item.source_end_time) }}s · {{ item.shot_size }} · {{ item.camera_movement }}</small>
          <p>{{ item.action }}</p>
          <small v-if="showEvidence" class="sequence-phase-source-note">以下来源画面作为本阶段证据，不代表每张都要生成一次切镜。</small>
          <p v-if="item.source_edit_times?.length && showEvidence">来源原切点：{{ item.source_edit_times.map(time => `${seconds(time)}s`).join('、') }}<span v-if="item.generation_edit_times?.length">；生成切镜安排：{{ item.generation_edit_times.map(time => `${seconds(time)}s`).join('、') }}</span></p>
          <button v-for="sourceId in (showEvidence ? stageSourceIds(item) : [])" :key="sourceId" type="button" class="outline-button" @click="emit('sourceShot', sourceId)">查看来源镜头 {{ members.find(shot => shot.id === sourceId)?.number }}</button>
        </li>
      </ol>
    </section>
    <section v-if="tab === 'all'" class="inspector-section" data-testid="sequence-prompts"><div class="section-title">此生成片段的 Prompt</div><article v-for="prompt in prompts" :key="prompt!.id" class="sequence-prompt"><h3>{{ prompt!.prompt_type === 'first_frame' ? '首帧图片 Prompt' : 'Seedance 视频 Prompt' }}</h3><small>{{ prompt!.platform }} · v{{ prompt!.version }}</small><textarea :value="prompt!.positive_prompt" readonly :aria-label="prompt!.prompt_type === 'first_frame' ? '片段首帧图片 Prompt' : '片段 Seedance 视频 Prompt'" /><button type="button" class="full-button" @click="emit('copy', prompt!.positive_prompt)"><Copy :size="14" />复制此片段 Prompt</button></article><p v-if="!prompts.length" class="sequence-note">此片段的 Prompt 尚未就绪。</p></section>
    <section v-if="tab === 'overview'" class="sequence-prompt-links" aria-label="片段提示词"><button v-for="item in prompts" :key="item!.id" type="button" @click="emit('openPrompt', item!.id)">{{ item!.prompt_type === 'first_frame' ? '查看首帧图片 Prompt' : '查看 Seedance Prompt' }}</button></section>
    <p v-if="tab === 'review' && !relationIncomplete && !sequence.warnings?.length" class="sequence-note">此片段未记录额外复核项。可以从证据页打开来源镜头，核对具体事实。</p>
    <details v-if="showEvidence" :open="tab === 'evidence'" class="inspector-section" data-testid="sequence-source-members"><summary>来源镜头 · {{ members.length }} 个</summary><button v-for="shot in members" :key="shot.id" class="sequence-member" type="button" @click="emit('sourceShot', shot.id)"><img v-if="shot.thumb" :src="shot.thumb" alt="" /><span><b>SH {{ String(shot.number).padStart(2, '0') }} · {{ shot.time }}</b><small>{{ shot.observedAction || shot.action }}</small></span></button></details>
    <details v-if="decisions.length && showEvidence" class="inspector-section" data-testid="merge-decisions"><summary>合并与保留边界的原因</summary><p v-for="decision in decisions" :key="`${decision.left_shot_id}:${decision.right_shot_id}`" class="sequence-note">{{ decision.merged ? '已合并' : '保留边界' }}：{{ decision.reason }}</p></details>
    <div v-if="sequence.warnings?.length && showReview" class="analysis-warning"><strong>片段需复核</strong><span v-for="warning in sequence.warnings" :key="warning">{{ warning }}</span></div>
  </div>
</template>

<style scoped>
.sequence-prompt-links{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.sequence-prompt-links button{padding:8px 11px;border:1px solid #7194aa44;border-radius:7px;background:#7194aa12;color:#b1d9ea;font:inherit;font-size:11px;cursor:pointer}
.sequence-phase-name{margin:0 0 6px;font-size:12px;line-height:1.6;color:var(--text,#e8eaf0)}
.sequence-phase-source-note{margin:6px 0}
.sequence-note{font-size:11px;color:var(--text-muted,#989eae);line-height:1.7}.sequence-timeline{list-style:none;padding:0}.sequence-timeline li{border-left:2px solid var(--accent,#9d83f4);padding:2px 0 14px 12px;margin-bottom:12px}.sequence-timeline b,.sequence-timeline small{display:block;font-size:11px;line-height:1.8}.sequence-timeline small{color:var(--text-muted,#989eae)}.sequence-timeline p{font-size:12px;line-height:1.7}.sequence-prompt{margin:15px 0}.sequence-prompt h3{font-size:13px;margin-bottom:6px}.sequence-prompt textarea{box-sizing:border-box;width:100%;min-height:180px;margin:10px 0;padding:12px;resize:vertical;background:var(--surface,#202126);color:var(--text,#e8eaf0);border:1px solid var(--border,#7194aa55);border-radius:8px;font:inherit;line-height:1.75}.sequence-member{display:flex;width:100%;gap:10px;padding:10px 0;border:0;background:none;color:inherit;text-align:left;cursor:pointer}.sequence-member img{width:48px;height:58px;object-fit:contain;border-radius:5px}.sequence-member b,.sequence-member small{display:block;font-size:11px;line-height:1.8}.sequence-member small{color:var(--text-muted,#989eae)}
</style>
