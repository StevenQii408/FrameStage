export type NodeKind = 'project' | 'script' | 'video' | 'scene' | 'shot' | 'sequence' | 'prompt'
export type EntityStatus = 'draft' | 'queued' | 'processing' | 'ready' | 'warning' | 'failed' | 'stale' | 'cancelled' | 'paused_memory_pressure' | 'interrupted'

export interface AnalysisReview {
  static?: { status: 'ready' | 'pending_review'; missing_fields?: string[] }
  dynamic?: { status: 'ready' | 'pending_review'; missing_fields?: string[] }
  video_ready?: boolean
  issues?: Array<{ scope: string; severity: 'review' | 'info'; code: string; message: string; field?: string }>
}

export interface ShotFrame {
  id: string
  url: string
  timestamp: number
  width: number
  height: number
}

export interface SpatialEvidence {
  foreground: string
  midground: string
  background: string
  relationships: string
  observable: boolean
}

export interface ActionEvidenceRef {
  frame_id: string
  timestamp: number
  description: string
  bbox?: number[] | null
  crop_artifact_id?: string | null
}

export interface ActionPhase {
  description: string
  sourceStepIndices: number[]
  evidence: ActionEvidenceRef[]
  confidence: number
  generationStartSeconds?: number | null
  generationEndSeconds?: number | null
}

export interface ActionPresentation {
  schema_version: 'action-presentation.v1'
  main_action: string
  accompanying_states: string[]
  phases: Array<{
    description: string
    start_seconds: number | null
    end_seconds: number | null
    evidence: ActionEvidenceRef[]
    source_step_indices: number[]
  }>
  timing_mode: 'none' | 'sequence' | 'generation-suggestion'
  source: 'model_summary' | 'source_steps' | 'phases' | 'unknown'
  warnings: string[]
  evidence_granularity: 'field' | 'item' | 'none'
}

export interface ActionDetails {
  primarySubject: string
  initialState: string
  steps: string[]
  interactionTarget: string
  finalState: string
  emotionChange: string
  evidence: string[]
  accompanyingStates?: string[]
  actionPhases?: ActionPhase[]
  stepEvidence?: ActionEvidenceRef[][]
}

export interface ShotContract {
  purpose: string
  firstFrameState: string
  primaryAction: string
  endState: string
  continuityAnchors: string[]
  shotSize: string
  lens: string
  cameraMovement: string
  lighting: string
  composition: string
  sourceEditTimes?: number[]
  generationDurationSeconds?: number
  editorialNote?: string
  aspectRatio?: string
  observedStyle?: string
  styleProfileVersion?: string
  generationSuggestions?: Record<string, string>
  delivery?: {
    schema_version?: string
    first_frame_intent?: string
    first_frame_prompt?: string
    seedance_video_prompt?: string
    settings?: string[]
    continuity_checks?: string[]
    action_presentation?: ActionPresentation
    style_provenance?: {
      resolution_status?: 'resolved' | 'partial' | 'unknown'
      application_status?: string
      quality_state?: { structure?: string; observation?: string; interpretation?: string; style?: string; anchor?: string }
    }
  }
}

export interface RawShot {
  id: string
  number: number
  startTime: number
  endTime: number
  duration: number
  boundaryMetrics: Record<string, unknown>
  decision: string
  reason: string
  narrativeShotId?: string
  generationShotIds?: string[]
}

export interface ActionAtom {
  id: string
  number: number
  startTime: number
  endTime: number
  duration: number
  sourceRawShotIds: string[]
  frameIds: string[]
  primarySubject: string
  initialState: string
  action: string
  interactionTarget: string
  finalState: string
  confidence: number
  warnings: string[]
  generationShotId?: string
}

export interface SceneGroup {
  id: string
  order: number
  title: string
  location?: string
  timeOfDay?: string
  shotIds: string[]
  coverFrameIds: string[]
  startTime: number
  endTime: number
  warningCount: number
  groupingMode: string
}

export interface ScriptDialogue {
  id: string
  speaker_id: string | null
  speaker_name: string
  text: string
  parenthetical?: string
}

export interface ScriptCharacterProfile {
  id: string
  name: string
  description: string
  aliases?: string[]
}

export interface ScriptDirection {
  schema_version: 'script-director.v3'
  event_id?: string
  title?: string
  shot_function?: string
  cut_reason?: string
  camera_angle?: string
  source_span_ids?: string[]
  dialogue?: ScriptDialogue[]
  sound_cues?: string[]
  creative_assumptions?: string[]
  character_profiles?: ScriptCharacterProfile[]
  duration_kind?: 'estimated-performance'
  continuity_notes?: string[]
  coverage?: Record<string, unknown>
  checks?: Record<string, unknown>
}

export interface Shot {
  id: string
  startTime?: number
  endTime?: number
  number: number
  time: string
  duration: string
  action: string
  observedAction?: string
  sourceText?: string
  shotSize: string
  lens: string
  movement: string
  lighting: string
  confidence: number
  thumb: string
  frames: ShotFrame[]
  analysisWarnings: string[]
  analysisReview?: AnalysisReview
  actionDetails: ActionDetails
  spatialEvidence: SpatialEvidence
  sourceRawShotIds: string[]
  sourceActionAtomIds?: string[]
  sceneGroupId: string
  cutReason?: string
  sourceEditTimes?: number[]
  generationDurationSeconds?: number
  sceneReason?: string
  visualStyle?: {
    medium: string
    characterDesign: string
    rendering: string
    colorPalette: string
    texture: string
    visibleEffects: string[]
    evidence: string[]
  }
  status: EntityStatus
  generationSequence?: GenerationSequence
  /** Text-script planning metadata; not observations from a source video. */
  script_direction?: ScriptDirection
}

export interface NarrativeEvent {
  id: string
  goal: string
  source_shot_ids: string[]
  initial_state: string
  final_state: string
  primary_subject: string
  interaction_target?: string
  location?: string
  participants?: string[]
  evidence?: ActionEvidenceRef[]
  status?: string
}

export interface SequencePrompt {
  id: string
  prompt_type: 'first_frame' | 'video'
  platform: string
  version: number
  positive_prompt: string
  negative_prompt?: string
  status: string
  contract?: Record<string, unknown> | null
}

export interface GenerationSequenceTimeline {
  /** Model-selected action goal, not an inferred source edit boundary. */
  semantic_phase?: string | null
  source_shot_id: string
  source_shot_ids?: string[]
  fact_ids?: string[]
  source_start_time: number
  source_end_time: number
  generation_start_seconds: number
  generation_end_seconds: number
  shot_size: string
  camera_movement: string
  source_edit_times: number[]
  generation_edit_times?: number[]
  transition: 'initial' | 'cut' | 'continuation'
  action: string
  action_steps?: string[]
  initial_state: string
  final_state: string
}

export interface GenerationSequence {
  id: string
  event_id: string
  scene_group_id?: string
  cover_frame_id?: string
  title?: string
  main_action?: string
  source_shot_ids: string[]
  source_raw_shot_ids?: string[]
  source_start_time: number
  source_end_time: number
  source_duration_seconds: number
  generation_duration_seconds?: number | null
  timeline: GenerationSequenceTimeline[]
  generation_mode?: 'continuous' | 'internal_cuts'
  first_frame_prompt?: SequencePrompt | null
  seedance_prompt?: SequencePrompt | null
  status: string
  version?: number
  build_hash?: string
  warnings?: string[]
}

export interface Prompt {
  id: string
  shotId: string
  type: 'first_frame' | 'image' | 'video'
  platform: string
  version: number
  text: string
  negativeText?: string
  status: EntityStatus
  templateId: string
  templateVersion: number
  warnings: string[]
  contract?: ShotContract
  sequenceId?: string
  sequenceContract?: Record<string, unknown> | null
  /** A merged event's canonical video prompt; presentation only, never a new backend result. */
  eventSummary?: boolean
}

export interface GraphNodeData {
  kind: NodeKind
  title: string
  status: EntityStatus
  summary?: string
  shot?: Shot
  sequence?: GenerationSequence
  prompt?: Prompt
  sceneId?: string
  shotCount?: number
  warningCount?: number
  timeRange?: string
  meta?: string
}

export interface CanvasPosition {
  x: number
  y: number
}

export interface WorkbenchTask {
  key: string
  jobId: string | null
  inputType: 'video' | 'script'
  status: string
  stage: string
  progress: number
  error: string | null
  videoFilename: string
  provider: string
  model: string
  segmentationProfile: string
  styleStrategy?: string
  directStyleAnalysisId?: string
  rawShotCount: number
  narrativeShotCount: number
  shots: Shot[]
  prompts: Prompt[]
  rawShots: RawShot[]
  actionAtoms?: ActionAtom[]
  sceneGroups?: SceneGroup[]
  narrativeEvents?: NarrativeEvent[]
  generationSequences?: GenerationSequence[]
  sequenceMetadata?: { mode?: 'preview' | 'persisted'; [key: string]: unknown }
  deliveryQuality?: DeliveryQuality
  preflight?: Record<string, unknown>
  sceneStyleReview?: Record<string, unknown>
  position: CanvasPosition
}

export interface DeliveryQuality {
  execution_state: string
  overall: 'ready' | 'partial' | 'pending' | 'failed'
  visual: { total: number; usable: number; ready: number; warning: number; failed: number; unknown: number; static_usable?: number; dynamic_usable?: number; video_usable?: number; static_only?: number; dynamic_review?: number; video_review?: number }
  aggregation: { state: string; total: number; ready: number; fallback: number; stale: number; covered_sources: number }
  style: { state: string }
  prompts: { total: number; ready: number; review: number; stale: number }
  issues: { code: string; message: string; count?: number }[]
}
