export type DetailKind = 'task' | 'shot' | 'sequence' | 'prompt' | 'scene'
export type DetailTab = 'overview' | 'evidence' | 'review'
export interface DetailTarget {
  projectId: string
  taskKey: string
  jobId?: string | null
  kind: DetailKind
  id: string
  returnSequenceId?: string
}
export interface DetailScene { id: string; title: string; shotIds: string[]; groupingMode?: string }
export interface DetailSourceRequest { taskKey: string; shotId: string; returnSequenceId?: string }
export function detailIdentity(target: DetailTarget): string {
  return JSON.stringify([target.projectId, target.taskKey, target.jobId || '', target.kind, target.id])
}
export function detailSourceRequest(target: DetailTarget, shotId: string): DetailSourceRequest {
  return { taskKey: target.taskKey, shotId, ...(target.kind === 'sequence' ? { returnSequenceId: target.id }
    : target.returnSequenceId ? { returnSequenceId: target.returnSequenceId } : {}) }
}
