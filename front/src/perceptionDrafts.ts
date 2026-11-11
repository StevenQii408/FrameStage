import type { PerceptionFieldGroup } from './api/perception'

export interface PerceptionDraft { fieldGroup: PerceptionFieldGroup; reason: string }
const drafts = new Map<string, PerceptionDraft>()
const MAX_DRAFTS = 128
export function perceptionDraftKey(projectId: string, taskKey: string, jobId: string, shotId: string): string {
  return JSON.stringify([projectId, taskKey, jobId, shotId])
}
export function readPerceptionDraft(key: string): PerceptionDraft | undefined {
  const value = drafts.get(key)
  return value ? { ...value } : undefined
}
/** Session-only user input, never model responses, evidence frames or localStorage. */
export function savePerceptionDraft(key: string, draft: PerceptionDraft): void {
  drafts.delete(key)
  drafts.set(key, { fieldGroup: draft.fieldGroup, reason: draft.reason.slice(0, 1000) })
  while (drafts.size > MAX_DRAFTS) drafts.delete(drafts.keys().next().value!)
}
export function clearPerceptionDrafts(): void { drafts.clear() }
