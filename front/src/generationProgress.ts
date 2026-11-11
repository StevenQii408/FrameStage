import type { GenerationKind, GenerationStatus } from './api/generations'

export interface GenerationProgressInput {
  progress?: number | null
  status: GenerationStatus
  kind: GenerationKind
  createdAt?: string
  updatedAt?: string
  startedAt?: string
}

export interface GenerationDisplayProgress {
  value: number | null
  estimated: boolean
  phase: string
  text: string
}

const phases: Record<GenerationStatus, string> = {
  blocked: '等待接入', queued: '排队中', submitting: '提交云端', running: '云端生成中',
  finalizing: '保存生成结果', succeeded: '生成完成', failed: '生成失败',
  cancelled: '已取消', interrupted: '任务已中断',
}

/** Presentation only: these estimates must never enter the task, SSE or saved history. */
export function generationDisplayProgress(input: GenerationProgressInput, now = Date.now()): GenerationDisplayProgress {
  const phase = phases[input.status]
  const display = (value: number | null, estimated = false): GenerationDisplayProgress => ({
    value, estimated, phase,
    text: value === null ? phase : `${phase} · ${estimated ? '预计 ' : ''}${value}%`,
  })
  if (input.status === 'succeeded') return display(100)
  const active = ['queued', 'submitting', 'running', 'finalizing'].includes(input.status)
  if (typeof input.progress === 'number' && Number.isFinite(input.progress) && input.progress >= 0 && input.progress <= 100) {
    // A provider reporting 100 before the result has been saved is not delivery.
    return display(Math.min(active ? 99 : 100, Math.floor(input.progress)))
  }
  if (!active) return display(null)
  if (input.status === 'queued') return display(0, true)

  // Current cloud adapters do not update updated_at on polls without progress;
  // it is therefore the latest phase transition. Prefer that over queue creation.
  const timestamp = [input.updatedAt, input.startedAt, input.createdAt]
    .map(value => value ? Date.parse(value) : NaN).find(Number.isFinite)
  if (timestamp === undefined || !Number.isFinite(now) || timestamp > now + 300_000) return display(null)
  const seconds = Math.max(0, (now - timestamp) / 1000)
  if (input.status === 'submitting') return display(Math.min(5, 1 + Math.floor(seconds / 3)), true)
  if (input.status === 'finalizing') return display(Math.min(99, 95 + Math.floor(seconds / 4)), true)

  // A bounded time-and-stage cue, not a predicted completion time. Slow cloud
  // tasks stay at 95 until an actual completion/error event arrives.
  const referenceSeconds = input.kind === 'image' ? 60 : 180
  return display(Math.min(95, Math.floor(5 + 90 * (1 - Math.exp(-seconds / (referenceSeconds * .65))))), true)
}
