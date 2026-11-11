export interface CanvasDraft {
  key: string
  inputType: 'video' | 'script'
  position: { x: number; y: number }
  scriptText: string
}

/** Old hot reloads appended the same stable draft key repeatedly. Restore once. */
export function recoverCanvasDrafts(value: unknown): { drafts: CanvasDraft[]; duplicates: number } {
  const drafts = new Map<string, CanvasDraft>()
  let duplicates = 0
  for (const raw of Array.isArray(value) ? value : []) {
    if (!raw || typeof raw !== 'object' || typeof raw.key !== 'string' || !raw.key.startsWith('draft-')
      || !['script', 'video'].includes(raw.inputType) || !Number.isFinite(raw.position?.x) || !Number.isFinite(raw.position?.y)) continue
    const candidate: CanvasDraft = { key: raw.key, inputType: raw.inputType,
      position: { x: raw.position.x, y: raw.position.y }, scriptText: typeof raw.scriptText === 'string' ? raw.scriptText.slice(0, 10000) : '' }
    const previous = drafts.get(candidate.key)
    if (previous) {
      duplicates += 1
      if (!candidate.scriptText && previous.scriptText) candidate.scriptText = previous.scriptText
    }
    drafts.set(candidate.key, candidate)
  }
  return { drafts: [...drafts.values()], duplicates }
}

/** A server repair of identical drafts is not a competing creative edit. */
export function canvasEqualAfterDraftRecovery(left: Record<string, unknown>, right: Record<string, unknown>): boolean {
  const stable = (value: unknown): unknown => Array.isArray(value) ? value.map(stable)
    : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, stable(item)])) : value
  const normalize = (value: Record<string, unknown>) => Array.isArray(value.drafts)
    ? { ...value, drafts: [...new Map(value.drafts.map(item => [JSON.stringify(stable(item)), item])).values()] } : value
  return JSON.stringify(stable(normalize(left))) === JSON.stringify(stable(normalize(right)))
}
