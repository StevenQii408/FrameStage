<script setup lang="ts">
import { computed, ref } from 'vue'
import { Info } from 'lucide-vue-next'
import type { WorkbenchTask } from '../types'
import { timelineLanes, timelineTicks, timeLabel, type TimelineBasis } from '../timelineGeometry'
const props = defineProps<{ tasks: WorkbenchTask[]; selectedId: string }>()
const emit = defineEmits<{ select: [id: string]; details: [id: string] }>()
const basis = ref<TimelineBasis>('source')
const zoom = ref(52)
const lanes = computed(() => timelineLanes(props.tasks, basis.value))
const duration = computed(() => Math.max(1, ...lanes.value.map(lane => lane.duration)))
const ticks = computed(() => timelineTicks(duration.value, zoom.value))
const width = computed(() => Math.max(600, ticks.value[ticks.value.length - 1] * zoom.value + 100))
</script>

<template>
  <div class="timeline-editor" data-testid="timeline-editor">
    <div class="timeline-controls">
      <div class="timeline-basis" role="group" aria-label="时间基准">
        <button :aria-pressed="basis === 'source'" @click="basis = 'source'">来源时间线</button>
        <button :aria-pressed="basis === 'generation'" @click="basis = 'generation'">生成编排时间线</button>
      </div>
      <label>缩放 <input v-model.number="zoom" type="range" min="12" max="120" aria-label="时间轴缩放" />{{ zoom }} px/s</label>
      <button class="timeline-details" type="button" :disabled="!selectedId" aria-label="查看选中项详情" @click="emit('details', selectedId)"><Info :size="14" />查看详情</button>
      <span>{{ timeLabel(duration) }} · {{ basis === 'source' ? '视频使用原片时间；剧本为建议表演时间' : '按建议生成时长累计，不代表原片动作实测' }}</span>
    </div>
    <div class="timeline-scroll">
      <div class="timeline-content" :style="{ width: `${width + 180}px` }">
        <div class="time-ruler"><div class="time-lane-heading">任务 / 片段</div><div class="time-ruler-axis" :style="{ width: `${width}px` }"><span v-for="tick in ticks" :key="tick" :style="{ left: `${tick * zoom}px` }">{{ timeLabel(tick) }}</span></div></div>
        <section v-for="lane in lanes" :key="lane.task.key" class="time-lane" :aria-label="lane.task.videoFilename || lane.task.jobId || '剧本任务'">
          <div class="time-lane-heading"><b>{{ lane.task.videoFilename || '文字剧本' }}</b><small>{{ lane.task.jobId }}</small><small>{{ lane.estimated ? '建议时间' : '原片时间' }} · {{ lane.clips.length }} {{ basis === 'source' ? '来源镜头' : '片段' }}</small></div>
          <div class="time-lane-axis" :style="{ width: `${width}px` }">
            <i v-for="tick in ticks" :key="tick" class="time-grid-line" :style="{ left: `${tick * zoom}px` }" />
            <button v-for="clip in lane.clips.filter(clip => clip.durationKnown)" :key="clip.shot.id" class="time-clip" :class="[`time-clip-${clip.shot.status}`, { selected: selectedId === clip.shot.id }]" :style="{ left: `${clip.start * zoom}px`, width: `${Math.max(1, (clip.end - clip.start) * zoom)}px` }" :title="`${clip.shot.action}\n${clip.start.toFixed(2)}–${clip.end.toFixed(2)}s`" @click="emit('select', clip.shot.id)">
              <img v-if="clip.shot.thumb" :src="clip.shot.thumb" alt="" loading="lazy" />
              <span><b>{{ clip.shot.generationSequence ? 'SEQ' : 'SH' }} {{ String(clip.shot.number).padStart(2, '0') }}</b><small>{{ clip.durationKnown ? `${(clip.end - clip.start).toFixed(2)}s` : '时长待确定' }}</small><em>{{ clip.shot.action }}</em></span>
            </button>
            <div v-if="lane.clips.some(clip => !clip.durationKnown)" class="unknown-clips" aria-label="时长待确定的片段"><button v-for="clip in lane.clips.filter(clip => !clip.durationKnown)" :key="clip.shot.id" type="button" @click="emit('select', clip.shot.id)">{{ clip.shot.id }} · 时长待确定</button></div>
          </div>
        </section>
        <p v-if="!lanes.length" class="timeline-empty">创建并分析任务后，在此查看时间与片段。</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline-details{display:flex;align-items:center;gap:6px;padding:8px 10px;border:1px solid #7194aa44;border-radius:7px;background:transparent;color:inherit;cursor:pointer}.timeline-details:disabled{opacity:.45;cursor:default}
.timeline-editor{display:flex;flex-direction:column;min-height:0;flex:1;background:var(--surface);color:var(--text)}.timeline-controls{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:18px 24px;border-bottom:1px solid #7194aa33;font-size:11px}.timeline-controls>span{color:#8b9ba7;font-size:10px}.timeline-controls label{display:flex;align-items:center;gap:8px}.timeline-controls input{width:95px;accent-color:#82bcd9}.timeline-basis{display:flex;gap:5px}.timeline-basis button{padding:8px 11px;border:1px solid #7194aa44;border-radius:7px;background:transparent;color:inherit;cursor:pointer}.timeline-basis button[aria-pressed=true]{background:#7194aa24;border-color:#82bcd9}.timeline-scroll{flex:1;min-height:0;overflow:auto;padding:0 24px 30px}.timeline-content{min-width:100%;position:relative}.time-ruler{display:flex;height:45px;position:sticky;top:0;z-index:3;background:var(--surface)}.time-ruler-axis{position:relative;flex-shrink:0}.time-lane-axis{position:relative;flex-shrink:0}.time-ruler-axis>span{position:absolute;top:15px;font:10px monospace;color:#93a9b9;border-left:1px solid #7194aa55;padding-left:5px}.time-lane{display:flex;position:relative;border-bottom:1px solid #7194aa33;min-height:132px}.time-lane-heading{position:sticky;left:-24px;flex:0 0 180px;box-sizing:border-box;padding:17px 14px 15px 0;background:var(--surface);z-index:2;overflow:hidden}.time-lane-heading b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;line-height:1.8}.time-lane-heading small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:9px monospace;color:#8b9ba7;line-height:1.9}.time-grid-line{position:absolute;inset-block:0;width:1px;background:#7194aa18;pointer-events:none}.time-clip{position:absolute;top:20px;box-sizing:border-box;height:90px;display:flex;align-items:center;gap:8px;text-align:left;padding:6px;overflow:hidden;border:1px solid #759bb866;background:#7194aa25;border-radius:7px;color:inherit;cursor:pointer}.time-clip:hover{border-color:#8ed5ee;box-shadow:0 0 0 1px #8ed5ee55}.time-clip.selected{border-color:#8ed5ee;box-shadow:0 0 0 1px #8ed5ee55}.time-clip img{width:52px;height:72px;object-fit:contain;border-radius:4px}.time-clip>span{min-width:0}.time-clip b{display:block;font-size:10px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;line-height:1.7}.time-clip small{display:block;font-size:10px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;line-height:1.7}.time-clip em{display:block;font-size:10px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden;line-height:1.7;font-style:normal;opacity:.8}.time-clip-warning{border-color:#c1996266;background:#c1996218}.time-clip-failed{border-color:#c1996266;background:#c1996218}.time-clip-stale{border-style:dashed;opacity:.65}.duration-unknown{border-style:dashed;opacity:.65}.timeline-empty{padding:25px;color:#8b9ba7}
</style>

<style scoped>
.time-lane:has(.unknown-clips){min-height:180px}.unknown-clips{position:absolute;top:118px;display:flex;gap:8px;align-items:center}.unknown-clips button{border:1px dashed #7194aa88;background:#7194aa12;color:inherit;border-radius:5px;padding:7px;font-size:10px;cursor:pointer}
</style>
