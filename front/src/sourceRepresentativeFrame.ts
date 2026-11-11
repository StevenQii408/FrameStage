import type { ActionEvidenceRef, Shot, ShotFrame } from './types'

/** Display only. Keep frames[0], frame-out and the canonical first-frame prompt intact. */
export function sourceRepresentativeFrame(shot: Shot): ShotFrame | undefined {
  const frames = new Map(shot.frames.map(frame => [frame.id, frame]))
  const match = (references: ActionEvidenceRef[]) => references.flatMap(reference => {
    if (!reference || typeof reference.frame_id !== 'string' || !Number.isFinite(reference.timestamp)
      || typeof reference.description !== 'string' || !reference.description.trim()) return []
    const frame = frames.get(reference.frame_id)
    // Backend evidence is rounded to milliseconds. Never borrow a nearby frame
    // or a frame owned by a different source shot.
    return frame?.url && Number.isFinite(frame.timestamp) && Math.abs(frame.timestamp - reference.timestamp) <= .001 + Number.EPSILON
      ? [frame] : []
  }).sort((a, b) => a.timestamp - b.timestamp)[0]
  const steps = match((shot.actionDetails.stepEvidence || []).flat())
  if (steps) return steps
  const phases = match((shot.actionDetails.actionPhases || []).flatMap(phase => phase.evidence))
  if (phases) return phases
  // Older observations carry validated evidence as "frame-id | 7.297s：...".
  // Parse the exact ID/time contract only; the prose does not select a subject.
  const legacy = (shot.actionDetails.evidence || []).flatMap(text => {
    const parsed = /^(\S+) \| (\d+(?:\.\d+)?)s[：:]\s*(.+)$/.exec(text)
    return parsed ? [{ frame_id: parsed[1], timestamp: Number(parsed[2]), description: parsed[3] }] : []
  })
  return match(legacy)
}
