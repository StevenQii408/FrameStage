<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, provide, ref, watch, type Ref } from 'vue'
import { brandName } from './brand'
import { Background } from '@vue-flow/background'
import { SelectionMode, VueFlow, useVueFlow, type Connection, type Edge as FlowEdge, type Node as FlowNode, type NodeChange } from '@vue-flow/core'
import { makeSceneGroups, semanticZoomLevel } from './graph'
import { layoutSceneBranches, SHOTS_PER_ROW, SHOT_ROW_GAP } from './sceneLayout'
import { estimateCardSize, type CardLod } from './cardGeometry'
import { placePromptDeck, PROMPT_WIDTH, PROMPT_HEIGHT } from './promptPlacement'
import { useWorkbenchStore } from './stores/workbench'
import ProjectNode from './components/ProjectNode.vue'
import ShotNode from './components/ShotNode.vue'
import SceneNode from './components/SceneNode.vue'
import EnergyEdge from './components/EnergyEdge.vue'
import GeneratorNode from './components/GeneratorNode.vue'
const GeneratorInlinePanel = defineAsyncComponent(() => import('./components/GeneratorInlinePanel.vue'))
import { connectionSources, createGeneratorResolutionGuard, detachConnectionsFromSources, physicalGeneratorHandles, rememberGeneratorReferences, resolveGenerator, validateGeneratorConnection, type GeneratorDraft, type GeneratorConnection } from './generatorGraph'
import { isReferenceAsset, selectGeneratorVersion } from './generatorVersions'
import { restoreGeneratorSnapshot } from './generatorUndo'
import { loadLocalImage } from './generatorStorage'
import { useGeneratorAssets } from './composables/useGeneratorAssets'
import { useGeneratorTasks } from './composables/useGeneratorTasks'
import { useGeneratorEdits } from './composables/useGeneratorEdits'
import { MAX_CANVAS_ZOOM, MIN_CANVAS_ZOOM, useCanvasGestures, zoomCanvasAt } from './composables/useCanvasGestures'
import { useCanvasUndo } from './composables/useCanvasUndo'
import { useNodeDetails } from './composables/useNodeDetails'
import TacticalMiniMap from './components/TacticalMiniMap.vue'
import WorkflowRail from './components/WorkflowRail.vue'
import TimelineView from './components/TimelineView.vue'
import DeliveryQualityPanel from './components/DeliveryQualityPanel.vue'
import PromptNode from './components/PromptNode.vue'
const NodeDetailsHost = defineAsyncComponent(() => import('./components/NodeDetailsHost.vue'))
import GenerationSequenceNode from './components/GenerationSequenceNode.vue'
import EventPromptNode from './components/EventPromptNode.vue'
import { eventPromptForShot, migrateSequenceUi, sequenceCards, sequenceMembers, sequencePromptForShot, sequencePrompts, type SequenceUi } from './generationSequences'
import { eventPromptSize, eventSlotSize, placeEventPrompt } from './eventPromptLayout'
import { layoutSequenceBranches, sequenceDeckSize, SEQUENCE_SOURCES_PER_ROW, SEQUENCE_ROW_GAP } from './sequenceBranchLayout'
const StyleStudio = defineAsyncComponent(() => import('./components/StyleStudio.vue'))
const DirectStyleStudio = defineAsyncComponent(() => import('./components/DirectStyleStudio.vue'))
const VisualBibleStudio = defineAsyncComponent(() => import('./components/VisualBibleStudio.vue'))
const EventAgentStudio = defineAsyncComponent(() => import('./components/EventAgentStudio.vue'))
import StatusPill from './components/StatusPill.vue'
import { Activity, AlignJustify, Check, ChevronDown, ChevronLeft, Clipboard, Clock3, Download, Film, Palette, Image as ImageIcon, Video as VideoIcon, Layers3, ListFilter, PanelRight, Search, Settings2, Sparkles, Table2, Type, Undo2, MousePointer2, Hand, Minus, Plus, Scan, Trash2, Info, FileText } from 'lucide-vue-next'
import { useJobSnapshots } from './composables/useJobSnapshots'
import { terminalEvents, useJobStreams, type JobStreamEvent } from './composables/useJobStream'
import { cancelJob, createScriptJob, createVideoJob, downloadJobExport, resumeJob } from './api/jobs'
import { setApiProjectId } from './api/client'
import { readJobIds, writeJobIds } from './urlState'
import { isBlankCanvasDoubleClick } from './canvasInteraction'
import { canvasShortcut, focusCanvasOnPointer, isEditableTarget, isTextEditingTarget, isTrueCanvasModal, keyboardEventTarget } from './canvasKeyboard'
import type { DetailSourceRequest, DetailTab } from './nodeDetails'
import { planCanvasDeletion } from './canvasSelection'
import { visibleCanvasPrompts } from './promptVisibility'
import { generatorPanelAnchor } from './generatorPanelGeometry'
import { generatorFrameSize } from './generatorGeometry'
import { frameMediaViewport } from './generatorPreviewGeometry'
import { defaultGenerationSettings } from './api/generations'
import { recoverCanvasDrafts } from './canvasRecovery'
import { applyDarkTheme } from './theme'
import { encodeCanvasState, readCanvasState } from './canvasPersistence'
import type { WorkbenchTask } from './types'

const props = withDefaults(defineProps<{ projectId?: string; projectName?: string; initialCanvas?: Record<string, unknown> }>(), { projectId: '', projectName: '' })
const emit = defineEmits<{ canvasChange: [state: Record<string, unknown>]; exitProject: [] }>()

type CanvasPoint = { x: number; y: number }
// Vue Flow adds selection to its runtime graph; our controlled input retains that state.
type Node = FlowNode & { selected?: boolean }
type Edge = FlowEdge & { selected?: boolean }
type CanvasHistoryAction =
  | { kind: 'create-task'; taskKey: string }
  | { kind: 'delete-task'; task: WorkbenchTask; index: number; positions: Record<string, CanvasPoint>; removedConnections: GeneratorConnection[] }
  | { kind: 'generator-snapshot'; generators: GeneratorDraft[]; connections: GeneratorConnection[] }
  | { kind: 'move-nodes'; positions: Record<string, CanvasPoint>; manualAuxiliaryIds: string[] }
  | { kind: 'delete-nodes'; tasks: { task: WorkbenchTask; index: number }[]; generators: { generator: GeneratorDraft; index: number }[]; removedConnections: GeneratorConnection[]; positions: Record<string, CanvasPoint>; hiddenNodeIds: string[] }
  | { kind: 'restore-hidden'; hiddenNodeIds: string[] }
  | { kind: 'layout'; positions: Record<string, CanvasPoint>; taskPositions: Record<string, CanvasPoint>; manualAuxiliaryIds: string[] }

const store = useWorkbenchStore()
setApiProjectId(props.projectId)
provide('agent-review-requests', computed(() => store.tasks.flatMap(task => Array.isArray(task.sequenceMetadata?.fact_review_requests) ? task.sequenceMetadata.fact_review_requests : [])))
const listBasis = ref<'sequences' | 'source'>('sequences')
const listRows = computed(() => store.tasks.flatMap(task => (listBasis.value === 'source' ? task.shots : store.taskCanvasShots(task)).map(shot => ({ shot, task }))).filter(({ shot }) => (!store.query || `${shot.action} ${shot.number}`.toLowerCase().includes(store.query.toLowerCase())) && (store.filter === 'all' || shot.status === store.filter)))
const generatedCount = computed(() => store.tasks.reduce((count, task) => count + (task.generationSequences?.length || 0), 0))
const vueFlow = useVueFlow()
const { fitView, screenToFlowCoordinate, setViewport } = vueFlow
const toast = ref('')
const styleStudioOpen = ref(false)
const eventAgentOpen = ref(false)
const eventAgentContext = ref<{ taskKey: string; jobId: string } | null>(null)
const styleStudioShot = ref('')
const styleImageBlob = ref<Blob | null>(null)
const styleImageAssetId = ref('')
const styleTargets = computed(() => store.tasks.filter((task) => task.jobId && task.status === 'completed').map((task) => ({ id: task.jobId!, label: task.videoFilename || `剧本 ${task.jobId}` })))
async function openImageStyle(generator: GeneratorDraft) {
  if (!generator.imageAssetId) return
  const blob = await loadLocalImage(generator.imageAssetId)
  if (!blob) { showToast('参考图片已失效，请重新导入'); return }
  styleImageBlob.value = blob
  styleImageAssetId.value = generator.imageAssetId
  styleStudioOpen.value = true
}
function closeStyleStudio() { styleStudioOpen.value = false; styleImageBlob.value = null; styleImageAssetId.value = '' }
async function onStyleApplied(jobId: string) {
  const task = store.tasks.find((item) => item.jobId === jobId)
  if (task) await store.loadJob(jobId, task.position, task.key)
  buildGraph()
  showToast('画风已确认，镜头 Prompt 已更新')
}
const copied = ref(false)
const topbarOpen = ref(false)
const leftRailOpen = ref(false)
function toggleChrome(part: 'top' | 'left') {
  const state = part === 'top' ? topbarOpen : leftRailOpen
  state.value = !state.value
}
const streams = useJobStreams()
const workbenchRoot = ref<HTMLElement>()
const canvasWrap = ref<HTMLElement>()
// Keep deep runtime reactivity, while avoiding recursive unwrapping of Vue Flow's
// VNode/component callback types. Application data still uses the declared node types.
const nodes = ref<Node[]>([]) as Ref<Node[]>
const edges = ref<Edge[]>([]) as Ref<Edge[]>
const positionCache = new Map<string, { x: number; y: number }>()
const manualAuxiliaryIds = ref(new Set<string>())
const layoutSignatures = new Map<string, string>()
const measuredCards = new Map<string, { width: number; height: number; lod: CardLod }>()
let geometryRefreshQueued = false
let canvasDisposed = false
const createMenu = ref<{ screen: { x: number; y: number }; flow: { x: number; y: number } } | null>(null)
const canvasZoom = ref(1)
const focusedScene = ref('')
const sceneUi = ref<Record<string, { open: boolean; page: number }>>({})
const sequenceUi = ref<SequenceUi>({})
const viewport = ref({ x: 0, y: 0, zoom: 1 })
useCanvasGestures({ canvasWrap, getViewport: () => viewport.value, setViewport: view => setViewport?.(view, { duration: 0 }),
  blocked: () => restoringCanvas.value || nodeDragInProgress || marqueeSelecting || styleStudioOpen.value || eventAgentOpen.value || Boolean(createMenu.value) })
const activeEffectKeys = computed(() => store.tasks.filter((task) => task.jobId && ['processing', 'queued'].includes(task.status)).map((task) => task.key))
const pageVisible = ref(!document.hidden)
const canvasBounds = ref({ left: 0, top: 0, width: window.innerWidth, height: window.innerHeight })
const draftInputs = ref<Record<string, string>>({})
const unloadedJobKeys = ref<Record<string, string>>({})
const hiddenJobIds = ref<string[]>([])
const restoringCanvas = ref(true)
let geometryObserver: ResizeObserver | undefined
let viewportSaveTimer: ReturnType<typeof setTimeout> | undefined
function measureCanvas() {
  const rect = canvasWrap.value?.getBoundingClientRect()
  if (rect) canvasBounds.value = { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
}
function observeCanvas() {
  geometryObserver?.disconnect()
  measureCanvas()
  if (typeof ResizeObserver !== 'undefined' && canvasWrap.value) {
    geometryObserver = new ResizeObserver(measureCanvas)
    geometryObserver.observe(canvasWrap.value)
  }
}
function onPageVisibility() { pageVisible.value = !document.hidden }
const history = ref<CanvasHistoryAction[]>([])
const dragStartPositions = new Map<string, CanvasPoint>()
const generators = ref<GeneratorDraft[]>([])
const generatorConnections = ref<GeneratorConnection[]>([])
const wirePulse = ref('')
const selectedWireId = ref('')
const selectedNodeIds = ref(new Set<string>())
const selectionTool = ref<'select' | 'hand'>('select')
const spaceHeld = ref(false)
const handMode = computed(() => selectionTool.value === 'hand' || spaceHeld.value)
const canvasHiddenNodeIds = ref<string[]>([])
let marqueeSelecting = false
let nodeDragInProgress = false
const generatorEditorId = ref('')
const generatorHistoryId = ref('')
const generatorPreviewId = ref('')
const details = useNodeDetails({ projectId: () => props.projectId, tasks: () => store.tasks,
  scene: (id, taskKey) => {
    const node = nodes.value.find(item => item.id === id && item.type === 'scene' && item.data?.taskKey === taskKey)
    return node ? { id, title: String(node.data.title || '场次'), shotIds: (node.data.shots || []).map((shot: { id: string }) => shot.id), groupingMode: node.data.groupingMode } : undefined
  } })
useCanvasUndo({ undo: () => { createMenu.value = null; undoCanvasAction() }, hasHistory: () => history.value.length > 0,
  enabled: () => !canvasDisposed && !restoringCanvas.value,
  modalOpen: () => styleStudioOpen.value || eventAgentOpen.value
    || Boolean(document.querySelector('[aria-modal="true"], [data-canvas-modal]:not([data-canvas-modal="false"])')) })
let draggedClickIds = new Set<string>()
function beginCanvasPointer(event?: PointerEvent) {
  draggedClickIds.clear()
  if (event) focusCanvasOnPointer(event, canvasWrap.value || null)
}
function releasePanKey(event?: Event) { if (!event || event.type === 'blur' || (event as KeyboardEvent).code === 'Space' || (event as KeyboardEvent).key === ' ') spaceHeld.value = false }
function onPanKeydown(event: KeyboardEvent) {
  if (event.key !== ' ' || event.repeat || event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey
    || store.activeView !== 'canvas' || isEditableTarget(event.target) || styleStudioOpen.value || eventAgentOpen.value || createMenu.value || generatorPreviewId.value) return
  // Capture before Vue Flow's Space handler, which prevents the default event.
  // Native button activation and keyboard activation of accessible cards stay local.
  if (event.target instanceof Element && event.target.closest('button,a,[role="button"]')) return
  spaceHeld.value = true
  event.preventDefault()
}
function captureCanvasClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target : null
  const selectionFrame = Boolean(target?.closest('.vue-flow__nodesselection'))
  const nodeElement = target?.closest('.vue-flow__node') || (selectionFrame
    ? document.elementsFromPoint?.(event.clientX, event.clientY).find(element => element.closest('.vue-flow__node'))?.closest('.vue-flow__node') : null)
  const nodeId = nodeElement?.getAttribute('data-id')
  if (!nodeId) return
  if (draggedClickIds.has(nodeId)) { draggedClickIds.clear(); event.stopPropagation(); return }
  if (selectionFrame && !event.shiftKey && !event.metaKey && !event.ctrlKey) { event.stopPropagation(); select(nodeId); return }
  if (!(event.shiftKey || event.metaKey || event.ctrlKey) || target?.closest('input,textarea,select,[contenteditable="true"],.vue-flow__handle,[role="dialog"]')) return
  event.preventDefault()
  event.stopPropagation()
  const next = new Set(selectedNodeIds.value)
  if (next.has(nodeId)) next.delete(nodeId); else next.add(nodeId)
  applyMultiSelection(next)
}
let magnetHandle: HTMLElement | null = null
function resetMagnet() {
  magnetHandle?.style.removeProperty('--magnet-x')
  magnetHandle?.style.removeProperty('--magnet-y')
  magnetHandle = null
}
function handleMagneticPointer(event: PointerEvent) {
  if (store.motionReduced) { resetMagnet(); return }
  const direct = event.target instanceof Element ? event.target.closest<HTMLElement>('.vue-flow__handle.magnetic-port') : null
  const nearby = document.elementsFromPoint?.(event.clientX, event.clientY).find((item) => item.classList.contains('magnetic-port')) as HTMLElement | undefined
  const handle = nearby || direct
  if (magnetHandle !== handle) resetMagnet()
  if (!handle) return
  magnetHandle = handle
  const rect = handle.getBoundingClientRect()
  const x = Math.max(-6, Math.min(6, (event.clientX - rect.left - rect.width / 2) * .38))
  const y = Math.max(-6, Math.min(6, (event.clientY - rect.top - rect.height / 2) * .38))
  handle.style.setProperty('--magnet-x', `${x.toFixed(1)}px`)
  handle.style.setProperty('--magnet-y', `${y.toFixed(1)}px`)
}

function snapshotGenerators() {
  pushHistory({ kind: 'generator-snapshot', generators: JSON.parse(JSON.stringify(generators.value)), connections: JSON.parse(JSON.stringify(generatorConnections.value)) })
}
const guardGeneratorResolution = createGeneratorResolutionGuard()
function resolvedGenerator(id: string) {
  const prompts = Object.fromEntries(store.allPrompts.map((prompt) => [prompt.id, prompt.text]))
  const frames = Object.fromEntries(store.connectionShots.filter((shot) => shot.frames.length).map((shot) => [shot.id, { url: shot.frames[0].url, width: shot.frames[0].width, height: shot.frames[0].height }]))
  const resolved = resolveGenerator(id, generators.value, generatorConnections.value, prompts, frames, assetUrls.value)
  return guardGeneratorResolution(id, resolved, () => {
    for (const reference of resolved.references || []) {
      const output = reference.sourceNodeId ? generationTasks.outputFor(reference.sourceNodeId) : null
      if (output?.mime_type.startsWith('image/')) {
        reference.url = output.url; reference.width = output.width; reference.height = output.height
        reference.identity = 'generated:' + output.id; reference.source = '已连接生成图片产物'
      }
    }
    const reference = resolved.references?.find(item => item.selected && item.firstFrame) || resolved.references?.find(item => item.selected)
    if (reference) {
      resolved.imageUrl = reference.url; resolved.imageWidth = reference.width; resolved.imageHeight = reference.height
      resolved.imageIdentity = reference.identity; resolved.imageSource = reference.source
    }
    return resolved
  })
}
let generatorRefreshQueued = false
let generatorTopology = ''
const generatorChanged = () => {
  if (generatorPreviewId.value && !generators.value.some(item => item.id === generatorPreviewId.value)) closeGeneratorPreview()
  if (selectedWireId.value && !generatorConnections.value.some(wire => wire.id === selectedWireId.value)) selectedWireId.value = ''
  if (generators.value.some(item => item.id === generatorEditorId.value && isReferenceAsset(item))) generatorEditorId.value = ''
  if (generatorHistoryId.value && !generators.value.some(item => item.id === generatorHistoryId.value && item.versions?.length)) generatorHistoryId.value = ''
  persistCanvasState()
  if (generatorRefreshQueued || canvasDisposed) return
  generatorRefreshQueued = true
  nextTick(() => {
    generatorRefreshQueued = false
    if (canvasDisposed) return
    const signature = JSON.stringify([generators.value.map(item => [item.id, item.kind, item.position, item.imageWidth, item.imageHeight, item.nodeMode, item.activeVersionId, item.generationSettings?.aspect_ratio]), generatorConnections.value])
    if (signature !== generatorTopology) { generatorTopology = signature; buildGraph(); return }
    // Prompt edits and SSE percentage changes update generator cards only.
    nodes.value = nodes.value.map(node => {
      if (node.type !== 'generator') return node
      const item = generators.value.find(generator => generator.id === node.id)
      return item ? { ...node, data: generatorNodeData(item) } : node
    })
    edges.value = edges.value.map(edge => {
      if (!edge.data?.manual) return edge
      const wire = generatorConnections.value.find(item => item.id === edge.id)
      return wire ? { ...edge, data: { ...edge.data, active: wirePulse.value === wire.id || [wire.source, wire.target].some(id => ['queued', 'submitting', 'running', 'finalizing'].includes(generationTasks.jobs.value[id]?.status)) } } : edge
    })
  })
}
const { assetUrls, hydrateAssets, importGeneratorImage, removeGeneratorImage, removeGeneratorReference, addReferenceImage } = useGeneratorAssets(generators, generatorChanged, snapshotGenerators, showToast)
const generationTasks = useGeneratorTasks(generators, resolvedGenerator, generatorChanged, showToast, { projectId: () => props.projectId || null })
const generatorEdits = useGeneratorEdits({ generators, connections: generatorConnections,
  resolve: resolvedGenerator,
  sources: () => connectionSources(store.allPrompts, store.connectionShots, generators.value),
  snapshot: snapshotGenerators, changed: generatorChanged, notify: showToast,
  beforeRemove: id => { generationTasks.detach(id); if (store.selectedId === id) clearCanvasSelection() },
})
const changeGeneratorSettings = generatorEdits.settings
const changeGeneratorPrompt = generatorEdits.prompt
const disconnectGenerator = generatorEdits.disconnect
function deleteGenerator(id: string) { generatorEdits.remove(id); void hydrateAssets() }
function toggleGeneratorHistory(id: string) {
  closeGeneratorPreview()
  const item = generators.value.find(node => node.id === id)
  if (!item?.versions?.length || isReferenceAsset(item)) return
  const close = generatorHistoryId.value === id
  select(id)
  generatorEditorId.value = ''
  details.close()
  generatorHistoryId.value = close ? '' : id
  buildGraph()
}
function selectGeneratedVersion(value: { nodeId: string; versionId: string }) {
  closeGeneratorPreview()
  const item = generators.value.find(node => node.id === value.nodeId)
  if (!item?.versions?.some(version => version.id === value.versionId) || item.activeVersionId === value.versionId) return
  snapshotGenerators()
  if (selectGeneratorVersion(item, value.versionId)) {
    generatorChanged()
    showToast('已置顶此版本，连接的参考图已同步更新')
  }
}
function removeReference(nodeId: string, referenceId: string) {
  const reference = resolvedGenerator(nodeId).references?.find(item => item.id === referenceId)
  const wire = reference?.sourceNodeId && generatorConnections.value.find(item => item.target === nodeId && item.source === reference.sourceNodeId && item.targetHandle === 'image-in')
  if (wire) generatorEdits.removeConnection(wire.id)
  else removeGeneratorReference(nodeId, referenceId)
}

function pushHistory(action: CanvasHistoryAction) {
  history.value.push(action)
  if (history.value.length > 50) history.value.shift()
}

function taskNodeIds(task: WorkbenchTask) {
  return new Set([task.key, ...task.shots.map((shot) => shot.id), ...task.prompts.map((prompt) => prompt.id), ...sequenceCards(task).map(shot => shot.id), ...sequencePrompts(task).map(prompt => prompt.id)])
}

function snapshotPositions(predicate: (node: Node) => boolean = () => true) {
  return Object.fromEntries(nodes.value.filter(predicate).map((node) => [node.id, { ...node.position }]))
}

function clearTaskPositionCache(task: WorkbenchTask) {
  layoutSignatures.delete(task.key)
  const ids = taskNodeIds(task)
  for (const id of ids) measuredCards.delete(id)
  for (const id of [...positionCache.keys()]) {
    if (ids.has(id) || id.startsWith(`${task.key}:scene:`)) positionCache.delete(id)
  }
}

function projectStatus(task: WorkbenchTask) {
  if (task.status === 'completed') return task.deliveryQuality?.overall === 'partial' ? 'warning' : 'ready'
  if (['failed', 'cancelled', 'paused_memory_pressure', 'interrupted'].includes(task.status)) return task.status
  return task.status === 'draft' || task.status === 'queued' ? 'queued' : 'processing'
}

function shotNodeHeight(taskShot: WorkbenchTask['shots'][number]) {
  return shotNodeSize(taskShot).height
}
function shotNodeSize(shot: WorkbenchTask['shots'][number]) {
  const measured = measuredCards.get(shot.id)
  return measured?.lod === zoomLevel() ? measured : shot.generationSequence ? sequenceDeckSize(shot, zoomLevel()) : estimateCardSize(shot, zoomLevel())
}
function onNodeDimensions(changes: NodeChange[]) {
  if (marqueeSelecting) {
    const next = new Set(selectedNodeIds.value)
    for (const change of changes) if (change.type === 'select') {
      if (change.selected) next.add(change.id); else next.delete(change.id)
    }
    selectedNodeIds.value = next
  }
  let changed = false
  for (const change of changes) {
    if (change.type !== 'dimensions' || !change.dimensions) continue
    if (![change.dimensions.width, change.dimensions.height].every(value => Number.isFinite(value) && value > 0)) continue
    const node = nodes.value.find(item => item.id === change.id)
    if (!node || node.hidden || !['shot', 'sequence'].includes(node.type || '')) continue
    const width = Math.ceil(change.dimensions.width), height = Math.ceil(change.dimensions.height)
    const previous = measuredCards.get(change.id)
    if (previous?.lod === zoomLevel() && previous.width === width && previous.height === height) continue
    measuredCards.set(change.id, { width, height, lod: zoomLevel() })
    changed = true
  }
  if (!changed || geometryRefreshQueued) return
  geometryRefreshQueued = true
  nextTick(() => { geometryRefreshQueued = false; if (!canvasDisposed) buildGraph() })
}

function zoomLevel() {
  return semanticZoomLevel(canvasZoom.value)
}

function generatorNodeData(item: GeneratorDraft) {
  const resolved = resolvedGenerator(item.id), output = generationTasks.outputFor(item.id)
  const mediaWidth = output?.width || resolved.ownImageWidth, mediaHeight = output?.height || resolved.ownImageHeight
  const baseFrame = generatorFrameSize(item, { width: mediaWidth, height: mediaHeight })
  const previewOpen = generatorPreviewId.value === item.id
  const task = generationTasks.jobs.value[item.id], stale = generationTasks.isStale(item.id)
  const visualState = task && !stale && ['running', 'submitting', 'finalizing'].includes(task.status) ? 'rendering' : task?.status === 'failed' ? 'failed' : resolved.ownImageUrl ? 'reference-ready' : 'idle'
  return { generator: item, resolved, reduced: store.motionReduced, visualState, task, stale, output, historyOpen: generatorHistoryId.value === item.id,
    previewOpen, baseFrame,
    layoutWidth: baseFrame.width, layoutHeight: baseFrame.height + 42 }
}

function closeGeneratorPreview() {
  if (!generatorPreviewId.value) return
  generatorPreviewId.value = ''
  buildGraph()
}
function toggleGeneratorPreview(id: string) {
  if (nodeDragInProgress || draggedClickIds.has(id)) return
  if (generatorPreviewId.value === id) { closeGeneratorPreview(); return }
  const item = generators.value.find(node => node.id === id)
  if (!item || !(generationTasks.outputFor(id) || resolvedGenerator(id).ownImageUrl)) return
  measureCanvas()
  const bounds = canvasBounds.value
  const panel = canvasWrap.value?.querySelector<HTMLElement>('.tactical-minimap')?.getBoundingClientRect()
  const corner = panel && panel.width > 0 && panel.height > 0
    ? { right: panel.right - bounds.left, top: panel.top - bounds.top } : undefined
  const next = frameMediaViewport(item.position, generatorNodeData(item).baseFrame, bounds, corner)
  if (!next) return
  closeGeneratorPreview()
  select(id)
  generatorEditorId.value = ''; generatorHistoryId.value = ''; details.close()
  generatorPreviewId.value = id
  viewport.value = next
  canvasZoom.value = next.zoom
  void setViewport?.(next, { duration: 0 })
  buildGraph()
  persistCanvasState()
}
async function captureGeneratorFrame(value: { nodeId: string; blob: Blob; width: number; height: number; timestamp: number; source: 'first' | 'current' | 'last' }) {
  const source = generators.value.find(item => item.id === value.nodeId)
  if (!source || !generationTasks.outputFor(source.id)?.mime_type.startsWith('video/')) return
  const size = generatorNodeData(source).baseFrame
  const position = { x: source.position.x + size.width + 50, y: source.position.y }
  const ratio = value.width > 0 && value.height > 0 ? value.width / value.height : 1
  const capturedSize = { width: 268 * Math.min(1, ratio), height: 268 / Math.max(1, ratio) }
  for (let attempt = 0; attempt < 120; attempt++) {
    const blocked = nodes.value.filter(node => node.id !== source.id && !node.hidden).some(node => {
      const draft = generators.value.find(item => item.id === node.id)
      const box = draft ? { ...draft.position, ...generatorNodeData(draft).baseFrame }
        : { ...node.position, width: Number(node.data?.layoutWidth) || 250, height: Number(node.data?.layoutHeight) || 230 }
      return position.x < box.x + box.width + 24 && position.x + capturedSize.width + 24 > box.x
        && position.y < box.y + box.height + 52 && position.y + capturedSize.height + 52 > box.y
    })
    if (!blocked) break
    position.y += capturedSize.height + 62
  }
  const name = { first: '首帧', current: '当前帧', last: '尾帧' }[value.source]
  const image = await addReferenceImage(new File([value.blob], `${name}-${value.timestamp.toFixed(3)}.png`, { type: 'image/png' }), position)
  if (image) { buildGraph(); showToast(`已截取${name}，新增图片参考节点 · ⌘Z 可撤回`) }
}

function buildGraph(forceLayout = false) {
  if (nodeDragInProgress || marqueeSelecting) return
  if (selectedWireId.value && !generatorConnections.value.some((wire) => wire.id === selectedWireId.value)) selectedWireId.value = ''
  const nextNodes: Node[] = []
  const nextEdges: Edge[] = []
  const promptDecks: { shotId: string; sceneId: string; promptIds: string[] }[] = []
  const eventPromptPairs: { shotId: string; promptId: string }[] = []
  const previousPositions = new Map(nodes.value.map((node) => [node.id, node.position]))
  const relayoutKeys = new Set<string>()
  for (const task of store.tasks) {
    const prompts = store.taskCanvasPrompts(task)
    const canvasShots = store.taskCanvasShots(task)
    if (canvasShots[0]?.generationSequence) sequenceUi.value = migrateSequenceUi(task, sceneUi.value, sequenceUi.value)
    const signature = canvasShots[0]?.generationSequence ? JSON.stringify([task.position, zoomLevel(), canvasShots.map(shot => [shot.id, !!sequenceUi.value[shot.id]?.open,
      shotNodeSize(shot), sequenceMembers(shot.generationSequence!, task.shots).map(source => [source.id, shotNodeSize(source)]), sequencePromptForShot(shot, prompts)?.id])])
      : JSON.stringify([task.position, makeSceneGroups(canvasShots, store.taskCanvasSceneGroups(task)).map(scene => [scene.id, !!sceneUi.value[`${task.key}:scene:${scene.id}`]?.open, scene.shots.map(shot => {
      const size = eventSlotSize(shotNodeSize(shot), zoomLevel(), Boolean(eventPromptForShot(shot, prompts)))
      return [shot.id, size.width, size.height]
    })])])
    if (forceLayout || layoutSignatures.get(task.key) !== signature) relayoutKeys.add(task.key)
    layoutSignatures.set(task.key, signature)
  }
  const stablePosition = (taskKey: string, id: string, calculated: { x: number; y: number }) => forceLayout ? calculated : (positionCache.get(id) || (!relayoutKeys.has(taskKey) ? previousPositions.get(id) : undefined) || calculated)
  store.tasks.forEach((task, taskIndex) => {
    const status = projectStatus(task)
    const anchor = task.position
    const rootCalculated = { x: anchor.x, y: anchor.y }
    nextNodes.push({
      id: task.key,
      type: 'project',
      position: stablePosition(task.key, task.key, rootCalculated),
      data: {
        taskKey: task.key, index: taskIndex + 1, inputType: task.inputType,
        title: task.inputType === 'script' ? '剧本输入 · 分镜解析' : '视频输入 · 视觉分析',
        summary: task.error || (task.inputType === 'script' ? '剧本正在拆分为结构化分镜' : task.provider === 'bailian' ? '本地切镜 · 云端视觉解析' : '本地视觉分析任务'),
        stage: task.stage, progress: task.progress, shotCount: task.shots.length, sequenceCount: store.taskCanvasShots(task)[0]?.generationSequence ? store.taskCanvasShots(task).length : 0, filename: task.videoFilename,
        jobId: task.jobId, error: task.error, provider: task.provider, status, executionStatus: task.status, quality: task.deliveryQuality, draftScriptText: draftInputs.value[task.key] || '', layoutWidth: 280, layoutHeight: task.jobId ? 300 : task.inputType === 'script' ? 430 : 365,
        rawCutTimes: task.rawShots.slice(1).map(shot => shot.startTime),
      },
    })

    const canvasShots = store.taskCanvasShots(task)
    const canvasPrompts = store.taskCanvasPrompts(task)
    const selectedPrompt = canvasPrompts.find((prompt) => prompt.id === store.selectedId)
    const expandedShotId = canvasShots.some((shot) => shot.id === store.selectedId) ? store.selectedId : selectedPrompt?.shotId
    const scenes = makeSceneGroups(canvasShots, store.taskCanvasSceneGroups(task))
    const otherTaskKeys = new Set(store.tasks.filter((item) => item.key !== task.key).map((item) => item.key))
    const obstacles = [
      ...store.tasks.filter((item) => item.key !== task.key).map((item) => ({ ...item.position, width: 300, height: 450 })),
      ...nodes.value.filter((node) => !node.hidden && otherTaskKeys.has(String(node.data?.taskKey || ''))).map((node) => ({ ...node.position, width: Number(node.data?.layoutWidth) || 250, height: Number(node.data?.layoutHeight) || 230 })),
      ...nextNodes.filter((node) => !node.hidden && otherTaskKeys.has(String(node.data?.taskKey || ''))).map((node) => ({ ...node.position, width: Number(node.data?.layoutWidth) || 250, height: Number(node.data?.layoutHeight) || 230 })),
      ...generators.value.map(item => {
        const size = generatorNodeData(item)
        return { ...item.position, width: size.baseFrame.width, height: size.baseFrame.height + 42 }
      }),
    ]
    if (canvasShots[0]?.generationSequence) {
      const branches = layoutSequenceBranches(anchor, canvasShots.map(shot => {
        const members = sequenceMembers(shot.generationSequence!, task.shots)
        const merged = shot.generationSequence!.source_shot_ids.length > 1
        const prompt = sequencePromptForShot(shot, canvasPrompts)
        const keepPositions = !forceLayout && !relayoutKeys.has(task.key)
        return { id: shot.id, open: merged && !!sequenceUi.value[shot.id]?.open, coverSize: shotNodeSize(shot),
          sourceSizes: merged ? members.map(shotNodeSize) : [], promptSize: prompt ? eventPromptSize(zoomLevel()) : undefined,
          promptPosition: keepPositions && prompt ? previousPositions.get(prompt.id) : undefined,
          coverPosition: forceLayout ? undefined : positionCache.get(shot.id) || (keepPositions ? previousPositions.get(shot.id) : undefined),
          sourcePositions: forceLayout ? undefined : merged ? members.map(source => positionCache.get(source.id) || (keepPositions ? previousPositions.get(source.id) : undefined)) : [] }
      }), obstacles)
      const sourceNodes = new Map<string, Node>()
      canvasShots.forEach(shot => {
        const sequence = shot.generationSequence!
        const members = sequenceMembers(sequence, task.shots)
        const merged = sequence.source_shot_ids.length > 1
        const open = merged && !!sequenceUi.value[shot.id]?.open
        const branch = branches[shot.id]
        const sceneTitle = task.sceneGroups?.find(scene => scene.id === shot.sceneGroupId)?.title
        nextNodes.push({ id: shot.id, type: 'sequence', position: branch.cover,
          data: { taskKey: task.key, shot, sources: members, branchId: shot.id, sceneId: shot.id, sceneTitle,
            expanded: open, lod: zoomLevel(), dimmed: false, layoutWidth: shotNodeSize(shot).width, layoutHeight: shotNodeHeight(shot) } })
        nextEdges.push({ id: `e-${task.key}-${shot.id}`, source: task.key, target: shot.id, type: 'energy',
          data: { active: ['processing', 'queued'].includes(task.status) }, class: 'sequence-branch-edge', selectable: false })
        members.forEach((sourceShot, sourceIndex) => {
          const sourceNode: Node = { id: sourceShot.id, type: 'shot', hidden: !open,
            position: merged ? branch.sources[sourceIndex] : { ...branch.cover },
            data: { taskKey: task.key, shot: sourceShot, branchId: shot.id, sceneId: shot.id, sourceEvidence: true,
              lod: zoomLevel(), dimmed: false, layoutWidth: shotNodeSize(sourceShot).width, layoutHeight: shotNodeHeight(sourceShot) } }
          if (!sourceNodes.has(sourceShot.id) || !sourceNode.hidden) sourceNodes.set(sourceShot.id, sourceNode)
          if (!open) return
          const previous = members[sourceIndex - 1]
          const rowWrap = sourceIndex > 0 && sourceIndex % SEQUENCE_SOURCES_PER_ROW === 0
          const previousRow = rowWrap ? members.slice(sourceIndex - SEQUENCE_SOURCES_PER_ROW, sourceIndex) : []
          nextEdges.push({ id: `e-source-${shot.id}-${sourceShot.id}`, source: previous?.id || shot.id, target: sourceShot.id,
            type: 'energy', class: 'timeline-edge source-evidence-edge', selectable: false,
            data: { chronological: true, rowWrap,
              wrapY: rowWrap ? Math.max(...previousRow.map((source, index) => branch.sources[sourceIndex - SEQUENCE_SOURCES_PER_ROW + index].y + shotNodeHeight(source))) + SEQUENCE_ROW_GAP / 2 : undefined,
              wrapSourceIds: previousRow.map(source => source.id), rowGap: SEQUENCE_ROW_GAP,
              nextLabel: `SH ${String(sourceShot.number).padStart(2, '0')}` } })
        })
        const prompt = sequencePromptForShot(shot, canvasPrompts)
        if (prompt && branch.prompt) {
          const size = eventPromptSize(zoomLevel())
          nextNodes.push({ id: prompt.id, type: 'eventPrompt', position: branch.prompt,
            data: { taskKey: task.key, sceneId: shot.id, branchId: shot.id, sourceShotId: shot.id, prompt,
              sequenceNumber: shot.number, sourceCount: sequence.source_shot_ids.length, title: shot.action,
              lod: zoomLevel(), dimmed: false, layoutWidth: size.width, layoutHeight: size.height } })
          nextEdges.push({ id: `e-event-${prompt.id}`, source: open && members.length ? members[members.length - 1].id : shot.id,
            target: prompt.id, targetHandle: 'event-in', type: 'energy', selectable: false, class: 'event-prompt-edge', data: { eventPrompt: true } })
        }
        if (expandedShotId === shot.id) {
          const deckPrompts = visibleCanvasPrompts(canvasPrompts).filter(item => item.shotId === shot.id && item.id !== prompt?.id)
          promptDecks.push({ shotId: shot.id, sceneId: shot.id, promptIds: deckPrompts.map(item => item.id) })
          deckPrompts.forEach((item, promptIndex) => {
            nextNodes.push({ id: item.id, type: 'prompt', position: { x: 0, y: 0 },
              data: { taskKey: task.key, prompt: item, promptIndex, compact: true, sceneId: shot.id, branchId: shot.id, dimmed: false, layoutWidth: PROMPT_WIDTH, layoutHeight: PROMPT_HEIGHT } })
            nextEdges.push({ id: `e-${task.key}-${item.id}`, source: shot.id, target: item.id, class: 'prompt-edge prompt-edge-deck', selectable: false })
          })
        }
      })
      // Keep all source identities for Inspector and existing generator wires.
      task.shots.forEach(shot => {
        const node = sourceNodes.get(shot.id)
        nextNodes.push(node || { id: shot.id, type: 'shot', hidden: true, position: { x: anchor.x + 600, y: anchor.y },
          data: { taskKey: task.key, shot, lod: zoomLevel(), layoutWidth: shotNodeSize(shot).width, layoutHeight: shotNodeHeight(shot) } })
      })
      return
    }
    const branches = layoutSceneBranches(anchor, scenes.map((scene) => {
      const sceneId = `${task.key}:scene:${scene.id}`
      const slots = scene.shots.map(shot => eventSlotSize(shotNodeSize(shot), zoomLevel(), Boolean(eventPromptForShot(shot, canvasPrompts))))
      return { id: sceneId, open: Boolean(sceneUi.value[sceneId]?.open), shotHeights: slots.map(size => size.height), shotWidths: slots.map(size => size.width) }
    }), obstacles)
    scenes.forEach((scene) => {
      const sceneId = `${task.key}:scene:${scene.id}`
      const dimmed = false
      const state = sceneUi.value[sceneId] || { open: false, page: 1 }
      const allShots = scene.shots
      const branch = branches[sceneId]
      const scenePosition = branch.scene
      nextNodes.push({
        id: sceneId,
        type: 'scene',
        position: manualAuxiliaryIds.value.has(sceneId) ? stablePosition(task.key, sceneId, scenePosition)
          : !forceLayout && !relayoutKeys.has(task.key) ? previousPositions.get(sceneId) || scenePosition : scenePosition,
        data: { taskKey: task.key, sceneId, index: scene.index, title: scene.title, shots: scene.shots, sequenceMode: !!canvasShots[0]?.generationSequence, timeRange: `${scene.startTime}–${scene.endTime}`, warningCount: scene.warningCount, progress: task.progress, expanded: state.open, groupingMode: scene.groupingMode, analysisPaused: task.status === 'paused_memory_pressure', lod: zoomLevel(), dimmed, layoutWidth: 250, layoutHeight: 230 },
      })
      nextEdges.push({ id: `e-${task.key}-${sceneId}`, source: task.key, target: sceneId, type: 'energy', data: { active: ['processing', 'queued'].includes(task.status) }, class: 'scene-edge', selectable: false })
      allShots.forEach((shot, shotIndex) => {
        const calculated = branch.shots[shotIndex]
        nextNodes.push({ id: shot.id, type: shot.generationSequence ? 'sequence' : 'shot', position: state.open ? stablePosition(task.key, shot.id, calculated) : calculated, hidden: !state.open, data: { taskKey: task.key, shot, sceneId, lod: zoomLevel(), dimmed, layoutWidth: shotNodeSize(shot).width, layoutHeight: shotNodeHeight(shot) } })
        const eventPrompt = eventPromptForShot(shot, canvasPrompts)
        if (eventPrompt) {
          const size = eventPromptSize(zoomLevel())
          nextNodes.push({ id: eventPrompt.id, type: 'eventPrompt', position: calculated, hidden: !state.open,
            data: { taskKey: task.key, sceneId, sourceShotId: shot.id, prompt: eventPrompt, sequenceNumber: shot.number,
              sourceCount: shot.generationSequence!.source_shot_ids.length, title: shot.action, lod: zoomLevel(), dimmed,
              layoutWidth: size.width, layoutHeight: size.height } })
          eventPromptPairs.push({ shotId: shot.id, promptId: eventPrompt.id })
          if (state.open) nextEdges.push({ id: `e-event-${eventPrompt.id}`, source: shot.id, target: eventPrompt.id,
            targetHandle: 'event-in', type: 'energy', selectable: false, class: 'event-prompt-edge', data: { eventPrompt: true } })
        }
        if (state.open) {
          const previousShot = allShots[shotIndex - 1]
          const previousPrompt = previousShot && eventPromptForShot(previousShot, canvasPrompts)
          const source = shotIndex === 0 ? sceneId : previousPrompt?.id || previousShot.id
          const rowWrap = shotIndex > 0 && shotIndex % SHOTS_PER_ROW === 0
          const previousRow = rowWrap ? nextNodes.filter(node => allShots.slice(shotIndex - SHOTS_PER_ROW, shotIndex).some(item => item.id === node.id)) : []
          const wrapY = rowWrap ? Math.max(...previousRow.map(node => node.position.y + Number(node.data.layoutHeight))) + SHOT_ROW_GAP / 2 : undefined
          nextEdges.push({ id: `e-${task.key}-${shot.id}`, source, sourceHandle: previousPrompt ? 'prompt-out' : undefined, target: shot.id, type: 'energy', data: { active: shot.status === 'processing', chronological: true, rowWrap, wrapY,
            wrapSourceIds: rowWrap ? allShots.slice(shotIndex - SHOTS_PER_ROW, shotIndex).flatMap(item => [item.id, eventPromptForShot(item, canvasPrompts)?.id].filter(Boolean)) : [],
            nextLabel: `${shot.generationSequence ? 'SEQ' : 'SH'} ${String(shot.number).padStart(2, '0')}` }, class: 'timeline-edge', selectable: false })
        }
      })
      const expandedIndex = state.open ? allShots.findIndex((shot) => shot.id === expandedShotId) : -1
      if (expandedIndex >= 0) {
        const featuredId = eventPromptForShot(allShots[expandedIndex], canvasPrompts)?.id
        const deckPrompts = visibleCanvasPrompts(canvasPrompts).filter((prompt) => prompt.shotId === expandedShotId && prompt.id !== featuredId)
        promptDecks.push({ shotId: expandedShotId!, sceneId, promptIds: deckPrompts.map(prompt => prompt.id) })
        deckPrompts.forEach((prompt, promptIndex) => {
          nextNodes.push({ id: prompt.id, type: 'prompt', position: { x: 0, y: 0 }, data: { taskKey: task.key, prompt, promptIndex, compact: true, sceneId, dimmed, layoutWidth: PROMPT_WIDTH, layoutHeight: PROMPT_HEIGHT } })
          nextEdges.push({ id: `e-${task.key}-${prompt.id}`, source: expandedShotId!, target: prompt.id, class: 'prompt-edge prompt-edge-deck', selectable: false })
        })
      }
    })
    // Source entities remain in the graph when sequence cards are projected.
    if (canvasShots[0]?.generationSequence) task.shots.forEach(shot => {
      nextNodes.push({ id: shot.id, type: 'shot', hidden: true, position: stablePosition(task.key, shot.id, { x: anchor.x + 600, y: anchor.y }), data: { taskKey: task.key, shot, sceneId: `${task.key}:scene:${shot.sceneGroupId}`, layoutWidth: 250, layoutHeight: shotNodeHeight(shot) } })
    })
  })
  const sceneForShot = new Map<string, string>()
  const shotForPrompt = new Map<string, string>()
  store.tasks.forEach((task) => {
    if (store.taskCanvasShots(task)[0]?.generationSequence) {
      sequenceCards(task).forEach(card => {
        sceneForShot.set(card.id, card.id)
        card.generationSequence!.source_shot_ids.forEach(id => sceneForShot.set(id, card.id))
      })
      task.prompts.concat(sequencePrompts(task)).forEach(prompt => shotForPrompt.set(prompt.id, prompt.shotId))
      return
    }
    const sceneGroups = makeSceneGroups(store.taskCanvasShots(task), store.taskCanvasSceneGroups(task))
    sceneGroups.forEach((scene) => scene.shots.forEach((shot) => sceneForShot.set(shot.id, task.key + ':scene:' + scene.id)))
    const cards = sequenceCards(task)
    makeSceneGroups(task.shots, task.sceneGroups || []).forEach(scene => scene.shots.forEach(shot => {
      if (!sceneForShot.has(shot.id)) {
        const card = cards.find(item => item.generationSequence?.source_shot_ids.includes(shot.id))
        sceneForShot.set(shot.id, (card && sceneForShot.get(card.id)) || `${task.key}:scene:${scene.id}`)
      }
    }))
    cards.forEach(shot => { if (!sceneForShot.has(shot.id)) sceneForShot.set(shot.id, sceneForShot.get(shot.generationSequence!.source_shot_ids[0]) || `${task.key}:scene:${shot.sceneGroupId}`) })
    task.prompts.concat(sequencePrompts(task)).forEach((prompt) => shotForPrompt.set(prompt.id, prompt.shotId))
  })
  for (const item of generators.value) {
    const data = generatorNodeData(item)
    nextNodes.push({ id: item.id, type: 'generator', position: item.position, data,
      draggable: !data.previewOpen, zIndex: data.previewOpen ? 1000 : 0, class: data.previewOpen ? 'generator-preview-node' : '' })
  }
  const nodeRect = (node: Node) => ({ ...node.position, width: Number(node.data?.layoutWidth) || 250, height: Number(node.data?.layoutHeight) || 230 })
  const placedEventPrompts: ReturnType<typeof nodeRect>[] = []
  for (const pair of eventPromptPairs) {
    const shot = nextNodes.find(node => node.id === pair.shotId)!
    const prompt = nextNodes.find(node => node.id === pair.promptId)!
    if (prompt.hidden) { prompt.position = { ...shot.position }; continue }
    const obstacles = nextNodes.filter(node => !node.hidden && !['prompt', 'eventPrompt'].includes(node.type || '')).map(nodeRect)
    const size = { width: Number(prompt.data.layoutWidth), height: Number(prompt.data.layoutHeight) }
    // Derived from the actual (possibly user-dragged) owner, not a stale Prompt cache.
    prompt.position = placeEventPrompt(nodeRect(shot), size, [...obstacles, ...placedEventPrompts])
    placedEventPrompts.push(nodeRect(prompt))
  }
  for (const edge of nextEdges) if (edge.data?.rowWrap) {
    const sources = new Set(edge.data.wrapSourceIds)
    const previousRow = nextNodes.filter(node => !node.hidden && sources.has(node.id))
    edge.data.wrapY = Math.max(...previousRow.map(node => node.position.y + Number(node.data.layoutHeight))) + (edge.data.rowGap || SHOT_ROW_GAP) / 2
  }
  for (const deck of promptDecks) {
    const shot = nextNodes.find(node => node.id === deck.shotId)!
    const obstacles = nextNodes.filter(node => !node.hidden && node.type !== 'prompt').map(nodeRect)
    const sceneCards = nextNodes.filter(node => !node.hidden && node.data?.sceneId === deck.sceneId && ['shot', 'sequence'].includes(node.type || '')).map(nodeRect)
    const positions = placePromptDeck(nodeRect(shot), sceneCards, obstacles)
    deck.promptIds.forEach((id, index) => { nextNodes.find(node => node.id === id)!.position = positions[index] })
  }
  const visibleIds = new Set(nextNodes.filter((node) => !node.hidden).map((node) => node.id))
  for (const wire of generatorConnections.value) {
    if (!visibleIds.has(wire.target)) continue
    let visualSource = wire.source
    if (!visibleIds.has(visualSource)) {
      const shotId = shotForPrompt.get(wire.source) || wire.source
      visualSource = visibleIds.has(shotId) ? shotId : (sceneForShot.get(shotId) || '')
    }
    if (!visibleIds.has(visualSource)) continue
    nextEdges.push({
      id: wire.id, source: visualSource, target: wire.target,
      sourceHandle: visualSource === wire.source ? physicalGeneratorHandles(wire).sourceHandle : undefined,
      targetHandle: 'input', type: 'energy', class: 'generator-edge', selected: selectedWireId.value === wire.id,
      data: { active: wirePulse.value === wire.id || [wire.source, wire.target].some(id => ['queued', 'submitting', 'running', 'finalizing'].includes(generationTasks.jobs.value[id]?.status)), manual: true },
    })
  }
  // Canonical selection survives SSE/geometry rebuilds and never follows a
  // stale Vue Flow selection from a different node or project.
  const hidden = new Set(canvasHiddenNodeIds.value)
  for (const node of nextNodes) {
    if (!forceLayout && manualAuxiliaryIds.value.has(node.id) && positionCache.has(node.id)) node.position = { ...positionCache.get(node.id)! }
    node.selected = !node.hidden && selectedNodeIds.value.has(node.id)
  }
  nodes.value = nextNodes.filter(node => !hidden.has(node.id))
  const available = new Set(nodes.value.map(node => node.id))
  edges.value = nextEdges.filter(edge => available.has(edge.source) && available.has(edge.target))
  selectedNodeIds.value = new Set([...selectedNodeIds.value].filter(id => nodes.value.some(node => node.id === id && !node.hidden)))
}
function select(id: string) {
  if (generatorPreviewId.value && generatorPreviewId.value !== id) closeGeneratorPreview()
  const nextDetail = details.describe(id)
  const previousDetail = details.target.value
  if (vueFlow.nodesSelectionActive) vueFlow.nodesSelectionActive.value = false
  selectedWireId.value = ''
  selectedNodeIds.value = new Set([id])
  store.select(id)
  generatorHistoryId.value = ''
  generatorEditorId.value = !generatorPreviewId.value && generators.value.some(item => item.id === id && !isReferenceAsset(item)) ? id : ''
  if (generatorEditorId.value) details.close()
  else if (previousDetail && nextDetail?.taskKey === previousDetail.taskKey) details.open(id, { taskKey: nextDetail.taskKey, tab: details.tab.value })
  else if (previousDetail) details.close()
  createMenu.value = null
  persistCanvasState()
  buildGraph()
}
function inspectSourceShot(value: string | DetailSourceRequest) {
  const id = typeof value === 'string' ? value : value.shotId
  const task = store.tasks.find(item => (typeof value === 'string' || item.key === value.taskKey) && item.shots.some(shot => shot.id === id))
  if (!task) return
  const owner = task?.generationSequences?.find(sequence => sequence.source_shot_ids.includes(id))
  if (owner && owner.source_shot_ids.length > 1) sequenceUi.value[owner.id] = { open: true }
  select(id)
  generatorEditorId.value = ''
  details.open(id, { taskKey: task.key, tab: 'evidence', returnSequenceId: typeof value === 'string' ? owner?.id : value.returnSequenceId || owner?.id })
}

function focusWorkbench() { nextTick(() => (canvasWrap.value || workbenchRoot.value)?.focus({ preventScroll: true })) }
function closeDetails() { details.close(); focusWorkbench() }
function openNodeDetails(id = store.selectedId, options: { taskKey?: string; tab?: DetailTab; returnSequenceId?: string } = {}) {
  if (!details.describe(id, options)) return
  generatorEditorId.value = ''
  select(id)
  details.open(id, options)
}
function openSelectedDetails() { if (selectedNodeIds.value.size <= 1) openNodeDetails() }
function openSelectedPrompt() {
  const prompt = selectedPrompt.value
  if (prompt && selectedNodeDetail.value) openNodeDetails(prompt.id, { taskKey: selectedNodeDetail.value.taskKey })
}
function returnToDetailSequence(value: { taskKey: string; sequenceId: string }) { openNodeDetails(value.sequenceId, { taskKey: value.taskKey }) }
function openEventAgent(value: { taskKey: string; jobId: string }) {
  const task = store.tasks.find(item => item.key === value.taskKey && item.jobId === value.jobId)
  if (!task || task.inputType !== 'video' || task.status !== 'completed') return
  eventAgentContext.value = value; eventAgentOpen.value = true
}
function openDetailStyle(value: { taskKey: string; shotId: string }) {
  const task = store.tasks.find(item => item.key === value.taskKey && item.shots.some(shot => shot.id === value.shotId))
  if (!task) return
  store.activeTaskKey = task.key; styleStudioShot.value = value.shotId; styleStudioOpen.value = true
}

function clearCanvasSelection() {
  closeGeneratorPreview()
  if (vueFlow.nodesSelectionActive) vueFlow.nodesSelectionActive.value = false
  selectedNodeIds.value = new Set()
  details.close()
  generatorEditorId.value = ''
  generatorHistoryId.value = ''
  selectedWireId.value = ''
  store.selectedId = ''
  for (const node of nodes.value) node.selected = false
  for (const edge of edges.value) edge.selected = false
}

function applyMultiSelection(ids: Set<string>) {
  closeGeneratorPreview()
  selectedNodeIds.value = new Set([...ids].filter(id => nodes.value.some(node => node.id === id && !node.hidden)))
  if (vueFlow.nodesSelectionActive) vueFlow.nodesSelectionActive.value = selectedNodeIds.value.size > 1
  details.close()
  generatorHistoryId.value = ''
  selectedWireId.value = ''; generatorEditorId.value = ''
  store.selectedId = selectedNodeIds.value.size === 1 ? [...selectedNodeIds.value][0] : ''
  for (const node of nodes.value) node.selected = selectedNodeIds.value.has(node.id)
  for (const edge of edges.value) edge.selected = false
  createMenu.value = null
  persistCanvasState()
}
function onMarqueeStart() { clearCanvasSelection(); marqueeSelecting = true }
function onMarqueeEnd() {
  marqueeSelecting = false
  applyMultiSelection(selectedNodeIds.value)
  if (vueFlow.nodesSelectionActive) vueFlow.nodesSelectionActive.value = selectedNodeIds.value.size > 0
  nextTick(() => buildGraph())
}

function zoomCanvas(factor: number) {
  const { width, height } = canvasBounds.value
  const current = viewport.value
  const next = zoomCanvasAt(current, { x: width / 2, y: height / 2 }, current.zoom * factor)
  if (next) void setViewport?.(next, { duration: 0 })
}

function handlePaneClick() {
  createMenu.value = null
  clearCanvasSelection()
  persistCanvasState()
  buildGraph()
}

function handleEdgeClick({ edge }: { edge: Edge }) {
  clearCanvasSelection()
  if (!generatorConnections.value.some((wire) => wire.id === edge.id)) {
    persistCanvasState()
    buildGraph()
    return
  }
  selectedWireId.value = edge.id
  store.selectedId = ''
  createMenu.value = null
  buildGraph()
}
function deleteSelectedWire() {
  if (!selectedWireId.value || !generatorConnections.value.some((wire) => wire.id === selectedWireId.value)) return
  generatorEdits.removeConnection(selectedWireId.value)
  selectedWireId.value = ''
  persistCanvasState()
  buildGraph()
  showToast('已断开连线 · ⌘Z 可撤回')
}
function handleNodeClick({ node }: { node: Node }) {
  if (draggedClickIds.has(node.id)) { draggedClickIds.clear(); return }
  // Selecting a scene may expose its details entry; SceneNode still owns expansion.
  select(node.id)
}
function copyText(text: string) {
  navigator.clipboard?.writeText(text); copied.value = true; toast.value = 'Prompt 已复制到剪贴板'
  setTimeout(() => { copied.value = false; toast.value = '' }, 1800)
}
function autoLayout() {
  closeGeneratorPreview()
  if (nodes.value.length) {
    pushHistory({
      kind: 'layout',
      positions: snapshotPositions(),
      taskPositions: Object.fromEntries(store.tasks.map((task) => [task.key, { ...task.position }])),
      manualAuxiliaryIds: [...manualAuxiliaryIds.value],
    })
  }
  positionCache.clear()
  manualAuxiliaryIds.value = new Set()
  let y = 90
  store.tasks.forEach((task) => {
    const position = { x: 80, y }
    store.updateTaskPosition(task.key, position)
    positionCache.set(task.key, position)
    const cards = store.taskCanvasShots(task)
    y += Math.max(520, cards[0]?.generationSequence ? cards.reduce((height, shot) => {
      const open = sequenceUi.value[shot.id]?.open && shot.generationSequence!.source_shot_ids.length > 1
      const members = sequenceMembers(shot.generationSequence!, task.shots)
      const rows = open ? Math.ceil(members.length / SEQUENCE_SOURCES_PER_ROW) : 0
      return height + Math.max(shotNodeHeight(shot), rows ? rows * (Math.max(...members.map(shotNodeHeight)) + SEQUENCE_ROW_GAP) - SEQUENCE_ROW_GAP : 0, eventPromptSize(zoomLevel()).height) + 64
    }, 160) : makeSceneGroups(cards, store.taskCanvasSceneGroups(task)).length * 290 + 160)
  })
  persistCanvasState()
  focusedScene.value = ''
  buildGraph(true)
  nextTick(() => fitView({ maxZoom: 1.6, padding: 0.14, duration: 500 }))
}
function onNodeDragStart({ node, nodes: group }: { node: Node; nodes?: Node[] }) {
  closeGeneratorPreview()
  nodeDragInProgress = true
  details.close()
  generatorHistoryId.value = ''
  generatorEditorId.value = ''; selectedWireId.value = ''
  const moving = group?.length ? group : selectedNodeIds.value.has(node.id) && selectedNodeIds.value.size > 1
    ? nodes.value.filter(item => !item.hidden && selectedNodeIds.value.has(item.id)) : [node]
  selectedNodeIds.value = new Set(moving.map(item => item.id))
  draggedClickIds = new Set(moving.map(item => item.id))
  store.selectedId = moving.length === 1 && node.type !== 'scene' ? node.id : ''
  const taskKey = String(node.data?.taskKey || '')
  if (taskKey) store.activeTaskKey = taskKey
  for (const item of nodes.value) item.selected = selectedNodeIds.value.has(item.id)
  dragStartPositions.clear()
  moving.forEach(item => dragStartPositions.set(item.id, { ...item.position }))
}
function onNodeDragStop({ node, nodes: group }: { node: Node; nodes?: Node[] }) {
  const moving = group?.length ? group : dragStartPositions.size > 1
    ? nodes.value.filter(item => dragStartPositions.has(item.id)) : [node]
  const before = Object.fromEntries(moving.flatMap(item => {
    const position = dragStartPositions.get(item.id)
    return position && (position.x !== item.position.x || position.y !== item.position.y) ? [[item.id, { ...position }]] : []
  }))
  if (Object.keys(before).length) pushHistory({ kind: 'move-nodes', positions: before, manualAuxiliaryIds: [...manualAuxiliaryIds.value] })
  for (const item of moving) {
    positionCache.set(item.id, { ...item.position })
    if (['scene', 'prompt', 'eventPrompt'].includes(item.type || '') && before[item.id]) manualAuxiliaryIds.value.add(item.id)
    const generator = generators.value.find(generator => generator.id === item.id)
    if (generator) generator.position = { ...item.position }
    if (store.tasks.some(task => task.key === item.id)) store.updateTaskPosition(item.id, item.position)
    if (item.data?.taskKey) layoutSignatures.delete(String(item.data.taskKey))
  }
  nodeDragInProgress = false
  dragStartPositions.clear()
  persistCanvasState()
  buildGraph()
}

function onViewportChange(next: { x: number; y: number; zoom: number }) {
  const before = zoomLevel()
  viewport.value = next
  canvasZoom.value = next.zoom
  if (before !== zoomLevel()) buildGraph()
  clearTimeout(viewportSaveTimer)
  viewportSaveTimer = setTimeout(flushViewportSave, 300)
}

function flushViewportSave() {
  if (viewportSaveTimer === undefined) return
  clearTimeout(viewportSaveTimer)
  viewportSaveTimer = undefined
  persistCanvasState()
}

function persistCanvasState() {
  if (restoringCanvas.value || canvasDisposed) return
  try {
    const submitted = store.submittedTasks
    const positions = Object.fromEntries(positionCache)
    const sequenceIds = new Set(submitted.flatMap(task => (task.generationSequences || []).map(sequence => sequence.id)))
    for (const task of submitted) positions[task.key] = positionCache.get(task.key) || task.position
    for (const item of generators.value) rememberGeneratorReferences(item, resolvedGenerator(item.id).references || [])
    const state = {
      jobKeys: { ...unloadedJobKeys.value, ...Object.fromEntries(submitted.map((task) => [task.jobId!, task.key])) },
      positions,
      manualAuxiliaryIds: [...manualAuxiliaryIds.value],
      sceneUi: sceneUi.value,
      sequenceUi: Object.fromEntries(Object.entries(sequenceUi.value).filter(([id]) => sequenceIds.has(id))),
      selectedId: store.selectedId,
      activeTaskKey: store.activeTaskKey,
      generationView: 'sequences' as const,
      generators: generators.value,
      generatorConnections: generatorConnections.value,
      layoutVersion: 'project-canvas.v2',
      hiddenJobIds: hiddenJobIds.value,
      hiddenNodeIds: canvasHiddenNodeIds.value,
      viewport: viewport.value,
      activeView: store.activeView,
      drafts: recoverCanvasDrafts(store.tasks.filter(task => !task.jobId).map(task => ({ key: task.key, inputType: task.inputType, position: task.position, scriptText: draftInputs.value[task.key] || '' }))).drafts,
    }
    if (props.projectId) emit('canvasChange', JSON.parse(JSON.stringify(state)))
    else localStorage.setItem('frameforge-canvas-v1', encodeCanvasState(state))
  } catch { /* private browsing may disable storage */ }
}

function focusScene(sceneId: string) {
  const scene = nodes.value.find((node) => node.id === sceneId)
  const taskKey = String(scene?.data?.taskKey || '')
  const wasOpen = Boolean(sceneUi.value[sceneId]?.open)
  sceneUi.value[sceneId] = { open: !wasOpen, page: 1 }
  focusedScene.value = wasOpen ? '' : sceneId
  if (taskKey) store.activeTaskKey = taskKey
  clearCanvasSelection()
  if (scene) { selectedNodeIds.value = new Set([sceneId]); store.selectedId = sceneId }
  persistCanvasState()
  buildGraph()
  if (!wasOpen) nextTick(() => {
    const shotIds = new Set((scene?.data?.shots || []).slice(0, SHOTS_PER_ROW * 2).map((shot: { id: string }) => shot.id))
    const targets = nodes.value.filter(node => !node.hidden && (node.id === sceneId || (node.data?.sceneId === sceneId && (shotIds.has(node.id) || shotIds.has(node.data?.sourceShotId)))))
    // Pan to the scene without crossing semantic-zoom thresholds and changing
    // the card geometry halfway through its unfolding animation.
    fitView({ nodes: targets.map(node => node.id), padding: 0.16, duration: 480, minZoom: canvasZoom.value, maxZoom: canvasZoom.value })
  })
}

function toggleSequence(id: string) {
  const task = store.tasks.find(item => item.generationSequences?.some(sequence => sequence.id === id))
  const sequence = task?.generationSequences?.find(item => item.id === id)
  if (!task || !sequence || sequence.source_shot_ids.length < 2) return
  sequenceUi.value[id] = { open: !sequenceUi.value[id]?.open }
  store.activeTaskKey = task.key
  persistCanvasState()
  buildGraph()
}

function showWholeMovie() {
  focusedScene.value = ''
  Object.keys(sceneUi.value).forEach((id) => { sceneUi.value[id] = { ...sceneUi.value[id], open: false } })
  Object.keys(sequenceUi.value).forEach((id) => { sequenceUi.value[id] = { open: false } })
  clearCanvasSelection()
  persistCanvasState()
  buildGraph()
  nextTick(() => fitView({ maxZoom: 1.6, padding: 0.14, duration: 500 }))
}

function updateUrl() {
  if (props.projectId) return
  const ids = store.submittedTasks.map((task) => task.jobId).filter((id): id is string => Boolean(id))
  window.history.replaceState({}, '', writeJobIds(window.location.href, ids))
}
function showToast(message: string, duration = 2200) { toast.value = message; setTimeout(() => { if (toast.value === message) toast.value = '' }, duration) }


async function exportData() {
  const jobId = store.activeTask?.jobId
  if (!jobId) { showToast('请先选择一个已提交任务'); return }
  try {
    const blob = await downloadJobExport(jobId)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `storyboard-${jobId}.json`; anchor.click(); URL.revokeObjectURL(url)
    showToast('Storyboard JSON 已导出')
  } catch (error) { showToast(error instanceof Error ? error.message : '导出失败') }
}

async function cancelTask(taskKey: string) {
  const task = store.tasks.find((item) => item.key === taskKey)
  if (!task?.jobId) return
  try { await cancelJob(task.jobId); store.applyEvent(task.jobId, 'job.cancelled', {}); streams.close(task.jobId); buildGraph(); showToast('任务已取消') }
  catch (error) { showToast(error instanceof Error ? error.message : '取消失败') }
}
async function resumeTask(taskKey: string) {
  const task = store.tasks.find((item) => item.key === taskKey)
  if (!task?.jobId) return
  try { await resumeJob(task.jobId); store.applyEvent(task.jobId, 'job.stage_changed', { stage: 'resuming', progress: task.progress }); connectTask(task.jobId); buildGraph(); showToast('任务已恢复') }
  catch (error) { showToast(error instanceof Error ? error.message : '恢复失败') }
}

const taskCancellations = new Map<string, Promise<void>>()
/** A local removal stops future paid work; failed cancellation leaves a visible retry target. */
function hideAndStopTask(task: WorkbenchTask, index = store.tasks.indexOf(task)) {
  if (!task.jobId) return
  const jobId = task.jobId
  streams.close(jobId)
  hiddenJobIds.value = [...new Set([...hiddenJobIds.value, jobId])]
  delete unloadedJobKeys.value[jobId]
  if (store.taskIsTerminal(task) || taskCancellations.has(jobId)) return
  const fallback = JSON.parse(JSON.stringify(task)) as WorkbenchTask
  const cancellation = Promise.resolve().then(() => cancelJob(jobId)).then(() => {
    if (canvasDisposed) return
    // Undo can restore the node while its cancellation is in flight. It must not look running.
    store.applyEvent(jobId, 'job.cancelled', {})
    streams.close(jobId)
    persistCanvasState(); buildGraph()
  }).catch(() => {
    if (canvasDisposed) return
    hiddenJobIds.value = hiddenJobIds.value.filter(id => id !== jobId)
    store.restoreTask(fallback, index < 0 ? store.tasks.length : index)
    connectTask(jobId)
    void store.loadJob(jobId, fallback.position, fallback.key).then(() => buildGraph()).catch(() => undefined)
    updateUrl(); persistCanvasState(); buildGraph()
    showToast('后台任务取消失败，已恢复节点，请重试取消；已发出的请求可能产生费用。', 5000)
  }).finally(() => taskCancellations.delete(jobId))
  taskCancellations.set(jobId, cancellation)
}

function deleteTask(taskKey: string) {
  const task = store.tasks.find((item) => item.key === taskKey)
  if (!task) return
  const index = store.tasks.indexOf(task)
  const taskCopy = JSON.parse(JSON.stringify(task)) as WorkbenchTask
  const deletedIds = taskNodeIds(task)
  const { kept, detached: removedConnections } = detachConnectionsFromSources(generatorConnections.value, deletedIds)
  pushHistory({ kind: 'delete-task', task: taskCopy, index, removedConnections, positions: snapshotPositions((node) => node.id === task.key || node.data?.taskKey === task.key || node.data?.sceneId?.startsWith(`${task.key}:`) || deletedIds.has(node.id)) })
  generatorConnections.value = kept
  hideAndStopTask(task, index)
  clearTaskPositionCache(task)
  store.removeTask(taskKey)
  if (focusedScene.value.startsWith(`${task.key}:`)) focusedScene.value = ''
  updateUrl()
  persistCanvasState()
  buildGraph()
  showToast('已移除任务并请求停止分析 · ⌘Z 恢复节点不会自动重启分析')
}

function deleteSelectedNodes() {
  const plan = planCanvasDeletion(selectedNodeIds.value, {
    roots: store.tasks.map(task => ({ id: task.key, descendantIds: new Set([...taskNodeIds(task), ...nodes.value.filter(node => node.data?.taskKey === task.key).map(node => node.id)]) })),
    generatorIds: generators.value.map(item => item.id), nodeIds: nodes.value.filter(node => !node.hidden).map(node => node.id),
  })
  const removedIds = new Set(plan.removedNodeIds)
  if (!removedIds.size) return
  const removedConnections = generatorConnections.value.filter(wire => removedIds.has(wire.source) || removedIds.has(wire.target))
  const removedTasks = store.tasks.flatMap((task, index) => plan.taskIds.includes(task.key) ? [{ task: JSON.parse(JSON.stringify(task)) as WorkbenchTask, index }] : [])
  const removedGenerators = generators.value.flatMap((generator, index) => plan.generatorIds.includes(generator.id) ? [{ generator: JSON.parse(JSON.stringify(generator)) as GeneratorDraft, index }] : [])
  pushHistory({ kind: 'delete-nodes', tasks: removedTasks, generators: removedGenerators,
    removedConnections: JSON.parse(JSON.stringify(removedConnections)), positions: snapshotPositions(node => removedIds.has(node.id)), hiddenNodeIds: plan.hiddenNodeIds })
  for (const { task, index } of removedTasks) {
    hideAndStopTask(task, index)
    clearTaskPositionCache(task)
    store.removeTask(task.key)
    if (focusedScene.value.startsWith(`${task.key}:`)) focusedScene.value = ''
  }
  for (const { generator } of removedGenerators) generationTasks.detach(generator.id)
  generators.value = generators.value.filter(item => !plan.generatorIds.includes(item.id))
  generatorConnections.value = generatorConnections.value.filter(wire => !removedIds.has(wire.source) && !removedIds.has(wire.target))
  canvasHiddenNodeIds.value = [...new Set([...canvasHiddenNodeIds.value, ...plan.hiddenNodeIds])]
  clearCanvasSelection()
  void hydrateAssets()
  updateUrl(); persistCanvasState(); buildGraph()
  showToast(`已移除选中节点 · ⌘Z 整组撤回${plan.hiddenNodeIds.length ? '；分析结果仍保留在镜头列表中' : ''}`, 3500)
}

function restoreHiddenNodes() {
  if (!canvasHiddenNodeIds.value.length) return
  pushHistory({ kind: 'restore-hidden', hiddenNodeIds: [...canvasHiddenNodeIds.value] })
  canvasHiddenNodeIds.value = []
  persistCanvasState(); buildGraph()
}

function undoCanvasAction() {
  const action = history.value.pop()
  if (!action) { showToast('没有可撤回的画布操作'); return }
  clearCanvasSelection()
  if (action.kind === 'delete-nodes') {
    canvasHiddenNodeIds.value = canvasHiddenNodeIds.value.filter(id => !action.hiddenNodeIds.includes(id))
    for (const { task, index } of [...action.tasks].sort((a, b) => a.index - b.index)) {
      hiddenJobIds.value = hiddenJobIds.value.filter(id => id !== task.jobId)
      store.restoreTask(task, index)
      if (task.jobId) {
        if (!store.taskIsTerminal(task)) connectTask(task.jobId)
        void store.loadJob(task.jobId, task.position, task.key).then(() => buildGraph()).catch(() => undefined)
      }
    }
    for (const { generator, index } of [...action.generators].sort((a, b) => a.index - b.index)) {
      if (!generators.value.some(item => item.id === generator.id)) generators.value.splice(index, 0, generator)
    }
    const wireIds = new Set(generatorConnections.value.map(wire => wire.id))
    generatorConnections.value.push(...action.removedConnections.filter(wire => !wireIds.has(wire.id)))
    Object.entries(action.positions).forEach(([id, position]) => positionCache.set(id, { ...position }))
    void hydrateAssets(); void generationTasks.restore()
    clearCanvasSelection()
  } else if (action.kind === 'restore-hidden') {
    canvasHiddenNodeIds.value = action.hiddenNodeIds
  } else if (action.kind === 'move-nodes') {
    manualAuxiliaryIds.value = new Set(action.manualAuxiliaryIds)
    for (const [id, position] of Object.entries(action.positions)) {
      positionCache.set(id, { ...position })
      const generator = generators.value.find(item => item.id === id)
      if (generator) generator.position = { ...position }
      if (store.tasks.some(task => task.key === id)) store.updateTaskPosition(id, position)
      const taskKey = nodes.value.find(node => node.id === id)?.data?.taskKey
      if (taskKey) layoutSignatures.delete(String(taskKey))
    }
  } else if (action.kind === 'generator-snapshot') {
    const restoredIds = new Set(action.generators.map(item => item.id))
    generators.value.forEach(item => { if (!restoredIds.has(item.id)) generationTasks.detach(item.id) })
    generators.value = restoreGeneratorSnapshot(action.generators, generators.value)
    generatorConnections.value = action.connections
    void hydrateAssets()
    void generationTasks.restore()
  } else if (action.kind === 'create-task') {
    const task = store.tasks.find((item) => item.key === action.taskKey)
    if (task) hideAndStopTask(task)
    if (task) clearTaskPositionCache(task)
    store.removeTask(action.taskKey)
  } else if (action.kind === 'delete-task') {
    hiddenJobIds.value = hiddenJobIds.value.filter(id => id !== action.task.jobId)
    store.restoreTask(JSON.parse(JSON.stringify(action.task)) as WorkbenchTask, action.index)
    generatorConnections.value.push(...action.removedConnections)
    Object.entries(action.positions).forEach(([id, position]) => positionCache.set(id, { ...position }))
    if (action.task.jobId) {
      if (!store.taskIsTerminal(action.task)) connectTask(action.task.jobId)
      store.loadJob(action.task.jobId, action.task.position, action.task.key).then(() => buildGraph()).catch(() => undefined)
    }
  } else {
    positionCache.clear()
    manualAuxiliaryIds.value = new Set(action.manualAuxiliaryIds)
    layoutSignatures.clear()
    Object.entries(action.positions).forEach(([id, position]) => positionCache.set(id, { ...position }))
    Object.entries(action.taskPositions).forEach(([id, position]) => store.updateTaskPosition(id, position))
  }
  updateUrl()
  persistCanvasState()
  buildGraph()
  showToast('已撤回上一步画布操作')
}

function onGlobalKeydown(event: KeyboardEvent) {
  if (styleStudioOpen.value || eventAgentOpen.value || createMenu.value) return
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return
  const keyboardTarget = keyboardEventTarget(event)
  if (!event.repeat && !event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === 'i' && selectedNodeDetail.value
    && !isTextEditingTarget(keyboardTarget) && !isTrueCanvasModal(keyboardTarget)
    && !(keyboardTarget instanceof Element && keyboardTarget.closest('[data-canvas-details]'))) {
    event.preventDefault(); openSelectedDetails(); return
  }
  if (isEditableTarget(keyboardTarget)) return
  if (event.key === 'Escape') {
    if (generatorPreviewId.value) { closeGeneratorPreview(); event.preventDefault(); return }
    if (details.target.value) closeDetails(); else { clearCanvasSelection(); persistCanvasState() }
    event.preventDefault(); return
  }
  if (store.activeView === 'canvas' && !event.repeat && !event.altKey) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a') {
      event.preventDefault(); applyMultiSelection(new Set(nodes.value.filter(node => !node.hidden && node.selectable !== false).map(node => node.id))); return
    }
    if (!event.metaKey && !event.ctrlKey && !event.shiftKey && ['v', 'h'].includes(event.key.toLowerCase())) {
      selectionTool.value = event.key.toLowerCase() === 'v' ? 'select' : 'hand'; event.preventDefault(); return
    }
  }
  const shortcut = canvasShortcut(event, Boolean(selectedWireId.value || selectedNodeIds.value.size))
  // Undo belongs exclusively to useCanvasUndo's capture/fallback route, including
  // its restore/modal guards; the bubbling handler only owns deletion.
  if (!shortcut || shortcut === 'undo') return
  event.preventDefault()
  if (selectedWireId.value) deleteSelectedWire()
  else deleteSelectedNodes()
}

const { refresh: refreshTask, stale: staleSnapshots } = useJobSnapshots(async jobId => {
  const task = store.tasks.find(item => item.jobId === jobId)
  if (!task) return
  const selectedWasVisible = store.taskCanvasShots(task).some(shot => shot.id === store.selectedId) || store.taskCanvasPrompts(task).some(prompt => prompt.id === store.selectedId)
  await store.loadJob(jobId)
  if (canvasDisposed || !store.tasks.some(item => item.jobId === jobId)) return
  // Preserve selection when a source card is replaced by its sequence.
  const card = store.taskCanvasShots(task).find(shot => shot.generationSequence?.source_shot_ids.includes(store.selectedId) || task.prompts.some(prompt => prompt.id === store.selectedId && shot.generationSequence?.source_shot_ids.includes(prompt.shotId)))
  if (selectedWasVisible && card && !store.taskCanvasShots(task).some(shot => shot.id === store.selectedId)) store.select(card.id)
  buildGraph()
  persistCanvasState()
}, jobId => store.tasks.some(task => task.jobId === jobId))

async function handleStreamEvent(jobId: string, event: JobStreamEvent) {
  if (canvasDisposed) return
  store.applyEvent(jobId, event.type, event.payload)
  buildGraph()
  if (terminalEvents.has(event.type) || ['capacity.ready', 'style.direct.baseline_ready', 'shot.analyzed', 'prompt.ready', 'sequence.ready', 'scene.regrouped', 'script.document_ready', 'script.window_started', 'script.window_ready', 'script.scene_ready', 'script.tool_finished'].includes(event.type)) {
    await refreshTask(jobId, terminalEvents.has(event.type))
  }
}
function connectTask(jobId: string) { streams.connect(jobId, (event) => handleStreamEvent(jobId, event)) }

const pendingSubmissions = new Set<string>()
function prepareLeave() {
  if (restoringCanvas.value) { showToast('正在恢复项目，请稍候再切换'); return false }
  if (pendingSubmissions.size) { showToast('任务正在创建，请等待提交完成后再切换项目'); return false }
  if (taskCancellations.size) { showToast('正在停止后台分析，请等待取消完成后再切换项目'); return false }
  persistCanvasState()
  return true
}
defineExpose({ prepareLeave })
async function startJob(taskKey: string, payload: { inputType: 'video' | 'script'; file?: File; scriptText: string }) {
  if (pendingSubmissions.has(taskKey) || !store.tasks.some(task => task.key === taskKey && !task.jobId)) return
  const submittedDraft = JSON.parse(JSON.stringify(store.tasks.find(task => task.key === taskKey)!)) as WorkbenchTask
  pendingSubmissions.add(taskKey)
  try {
    const created = payload.inputType === 'video' && payload.file ? await createVideoJob(payload.file, payload.scriptText, props.projectId || undefined) : await createScriptJob(payload.scriptText, props.projectId || undefined)
    if (canvasDisposed) return
    if (!store.tasks.some(task => task.key === taskKey)) {
      // A pending POST can finish after Delete/Undo. Persist its hidden membership as well.
      hideAndStopTask({ ...submittedDraft, jobId: created.job_id, status: 'queued' })
      persistCanvasState()
      return
    }
    await store.attachJob(taskKey, created.job_id)
    updateUrl(); persistCanvasState(); connectTask(created.job_id); buildGraph()
    showToast('任务已创建，正在生成节点')
  } catch (error) { showToast(error instanceof Error ? error.message : '任务创建失败', 2800) }
  finally { pendingSubmissions.delete(taskKey) }
}

function openCreateMenu(event: MouseEvent) {
  if (restoringCanvas.value) return
  const target = event.target as HTMLElement
  if (!isBlankCanvasDoubleClick(target)) return
  event.preventDefault()
  const rect = canvasWrap.value?.getBoundingClientRect()
  if (!rect) return
  clearCanvasSelection()
  createMenu.value = {
    screen: { x: event.clientX - rect.left, y: event.clientY - rect.top },
    flow: screenToFlowCoordinate({ x: event.clientX, y: event.clientY }),
  }
}
function createNode(inputType: 'video' | 'script' | 'image-generator' | 'video-generator') {
  if (!createMenu.value) return
  clearCanvasSelection()
  if (inputType === 'image-generator' || inputType === 'video-generator') {
    snapshotGenerators()
    const position = { ...createMenu.value.flow }
    const kind = inputType === 'image-generator' ? 'image' : 'video'
    const generationSettings = defaultGenerationSettings(kind)
    const frame = generatorFrameSize({ generationSettings, imageAssetId: null, imageWidth: 0, imageHeight: 0 })
    for (let attempt = 0; attempt < 20; attempt++) {
      const blockers = nodes.value.filter((node) => !node.hidden && position.x < node.position.x + (Number(node.data?.layoutWidth) || 250) + 24 && position.x + frame.width + 24 > node.position.x && position.y < node.position.y + (Number(node.data?.layoutHeight) || 230) + 24 && position.y + frame.height + 42 + 24 > node.position.y)
      if (!blockers.length) break
      position.x = Math.max(position.x + 40, ...blockers.map((node) => node.position.x + (Number(node.data?.layoutWidth) || 250) + 38))
    }
    const item: GeneratorDraft = { id: 'gen-' + crypto.randomUUID(), kind, position, manualPrompt: '', imageAssetId: null, imageWidth: 0, imageHeight: 0, generationSettings }
    generators.value.push(item)
    store.select(item.id)
    selectedNodeIds.value = new Set([item.id])
    generatorEditorId.value = item.id
    persistCanvasState()
  } else {
    const task = store.createDraft(inputType, createMenu.value.flow)
    selectedNodeIds.value = new Set([task.key])
    positionCache.set(task.key, task.position)
    pushHistory({ kind: 'create-task', taskKey: task.key })
  }
  createMenu.value = null
  buildGraph()
}
function onConnect(candidate: Connection) {
  const connection = generatorEdits.connect({ ...candidate, sourceHandle: candidate.sourceHandle ?? null, targetHandle: candidate.targetHandle ?? null })
  if (!connection) return
  clearCanvasSelection()
  selectedWireId.value = connection.id
  wirePulse.value = connection.id
  setTimeout(() => { if (wirePulse.value === connection.id) { wirePulse.value = ''; buildGraph() } }, 850)
  persistCanvasState()
  buildGraph()
}

const selected = computed(() => store.graphData(store.selectedId))
const selectedNodeDetail = computed(() => selectedNodeIds.value.size <= 1 ? details.describe(store.selectedId) : null)
const selectedPrompt = computed(() => {
  const target = selectedNodeDetail.value
  const task = target && store.tasks.find(item => item.key === target.taskKey)
  if (!task || !target) return undefined
  if (target.kind === 'prompt') return [...task.prompts, ...sequencePrompts(task)].find(item => item.id === target.id)
  const sequence = task.generationSequences?.find(item => item.source_shot_ids.includes(target.id))
  const id = sequence?.id || target.id
  const prompts = store.taskCanvasPrompts(task).filter(item => item.shotId === id)
  return prompts.find(item => item.type === 'video') || prompts[0]
})
const nodeActionAnchor = computed(() => {
  if (!selectedNodeDetail.value || store.activeView !== 'canvas' || nodeDragInProgress || details.target.value) return null
  const node = nodes.value.find(item => item.id === store.selectedId && !item.hidden)
  if (!node) return null
  const { width, height } = canvasBounds.value, view = viewport.value
  const x = node.position.x * view.zoom + view.x, y = node.position.y * view.zoom + view.y
  const nodeWidth = Number(node.data?.layoutWidth || 250) * view.zoom, nodeHeight = Number(node.data?.layoutHeight || 230) * view.zoom
  if (x + nodeWidth < 0 || x > width || y + nodeHeight < 0 || y > height) return null
  const actionWidth = selectedPrompt.value && selectedNodeDetail.value.kind !== 'prompt' ? 218 : 132
  const top = y > 100 ? y - 42 : y + nodeHeight + 8
  return { x: Math.max(12, Math.min(width - actionWidth - 12, x + nodeWidth / 2 - actionWidth / 2)),
    y: Math.max(66, Math.min(height - 66, top)), width: actionWidth }
})
const eventAgentTask = computed(() => eventAgentContext.value ? store.tasks.find(task => task.key === eventAgentContext.value!.taskKey && task.jobId === eventAgentContext.value!.jobId) : undefined)
const selectedGenerator = computed(() => generators.value.find((item) => item.id === store.selectedId))
const selectedGeneratorResolved = computed(() => selectedGenerator.value ? resolvedGenerator(selectedGenerator.value.id) : null)
const selectedGeneratorAnchor = computed(() => {
  if (generatorPreviewId.value || !selectedGenerator.value || isReferenceAsset(selectedGenerator.value) || generatorEditorId.value !== selectedGenerator.value.id || store.activeView !== 'canvas') return null
  const node = nodes.value.find(item => item.id === selectedGenerator.value!.id)
  if (!node) return null
  const bounds = canvasBounds.value, view = viewport.value
  const left = bounds.left + view.x + node.position.x * view.zoom, top = bounds.top + view.y + node.position.y * view.zoom
  const width = Number(node.data.layoutWidth) * view.zoom, height = Number(node.data.layoutHeight) * view.zoom
  if (left + width < bounds.left || left > bounds.left + bounds.width || top + height < bounds.top || top > bounds.top + bounds.height) return null
  return generatorPanelAnchor({ left, top, width, height }, { width: window.innerWidth, height: window.innerHeight })
})
const animatedEdgeIds = computed(() => {
  if (store.motionReduced || !pageVisible.value) return new Set<string>()
  const byId = new Map(nodes.value.map(node => [node.id, node]))
  const { x, y, zoom } = viewport.value, { width, height } = canvasBounds.value
  return new Set(edges.value.filter(edge => {
    if (!edge.data?.active) return false
    const a = byId.get(edge.source), b = byId.get(edge.target)
    if (!a || !b || a.hidden || b.hidden) return false
    const minX = Math.min(a.position.x, b.position.x) * zoom + x, maxX = (Math.max(a.position.x, b.position.x) + 300) * zoom + x
    const minY = Math.min(a.position.y, b.position.y) * zoom + y, maxY = (Math.max(a.position.y, b.position.y) + 300) * zoom + y
    return maxX >= 0 && minX <= width && maxY >= 0 && minY <= height
  }).slice(0, 24).map(edge => edge.id))
})
const activeAnalysisLabel = computed(() => {
  const task = store.activeTask
  if (!task) return '等待创建任务'
  if (!task.jobId) return '未提交草稿'
  const provider = task.provider && task.provider !== 'legacy-unknown' ? task.provider.toUpperCase() : '历史任务'
  return task.model ? `${task.model} · ${provider}` : provider
})

onMounted(async () => {
  applyDarkTheme()
  store.resetWorkspace()
  window.addEventListener('keydown', onGlobalKeydown)
  window.addEventListener('keydown', onPanKeydown, true)
  window.addEventListener('keyup', releasePanKey)
  window.addEventListener('blur', releasePanKey)
  document.addEventListener('visibilitychange', onPageVisibility)
  window.addEventListener('resize', measureCanvas)
  window.addEventListener('pagehide', flushViewportSave)
  const raw = props.initialCanvas || (() => { try { return JSON.parse(localStorage.getItem('frameforge-canvas-v1') || '{}') } catch { return {} } })()
  const saved = readCanvasState(JSON.stringify(raw))
  hiddenJobIds.value = Array.isArray(raw.hiddenJobIds) ? raw.hiddenJobIds.filter((id: unknown): id is string => typeof id === 'string') : []
  canvasHiddenNodeIds.value = Array.isArray(raw.hiddenNodeIds) ? [...new Set((raw.hiddenNodeIds as unknown[]).filter((id): id is string => typeof id === 'string' && id.length <= 320))] : []
  manualAuxiliaryIds.value = new Set(Array.isArray(raw.manualAuxiliaryIds) ? raw.manualAuxiliaryIds.filter((id: unknown): id is string => typeof id === 'string' && id.length <= 320) : [])
  unloadedJobKeys.value = { ...(saved?.jobKeys || {}) }
  sceneUi.value = saved?.sceneUi || {}
  sequenceUi.value = saved?.sequenceUi || {}
  // Older source-view persistence must never hide the actual merge result.
  store.generationView = 'sequences'
  generators.value = saved?.generators || []
  generatorConnections.value = saved?.generatorConnections || []
  // IndexedDB is shared by projects and undo histories. Never prune it from one canvas.
  void hydrateAssets().catch(() => undefined)
  for (const [id, point] of Object.entries(saved?.positions || {})) if (raw.layoutVersion === 'project-canvas.v2' || !id.includes(':scene:')) positionCache.set(id, point)
  const recovered = recoverCanvasDrafts(raw.drafts)
  if (recovered.duplicates) {
    try { localStorage.setItem(`frameforge-canvas-recovery:${props.projectId || 'legacy'}:v1`, JSON.stringify(raw)) } catch { /* server data remains intact if browser storage is unavailable */ }
  }
  for (const draft of recovered.drafts) {
    store.createDraft(draft.inputType, draft.position, draft.key)
    draftInputs.value[draft.key] = draft.scriptText
  }
  store.motionReduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches || false
  const backendIds = Array.isArray(raw.backendJobIds) ? raw.backendJobIds.filter((id: unknown): id is string => typeof id === 'string') : []
  const requested = props.projectId ? [...new Set([...Object.keys(saved?.jobKeys || {}), ...backendIds])].filter(id => !hiddenJobIds.value.includes(id)) : readJobIds(window.location.search)
  if (requested.length) {
    const results = await Promise.allSettled(requested.map((jobId, index) => {
      const key = saved?.jobKeys[jobId] || `task-${jobId}`
      return store.loadJob(jobId, saved?.positions[key] || { x: 80, y: 90 + index * 560 }, key)
    }))
    if (canvasDisposed) return
    results.forEach((result, index) => { if (result.status === 'fulfilled') delete unloadedJobKeys.value[requested[index]]; else unloadedJobKeys.value[requested[index]] ||= `task-${requested[index]}` })
    results.forEach((result, index) => { if (result.status === 'fulfilled' && !store.taskIsTerminal(result.value)) connectTask(requested[index]) })
    if (results.some((result) => result.status === 'rejected')) showToast('部分后端任务读取失败，请检查服务或任务 ID', 3000)
  }
  if (saved?.activeTaskKey && store.tasks.some((task) => task.key === saved.activeTaskKey)) store.activeTaskKey = saved.activeTaskKey
  // Restore data and positions, but never arm a stale destructive selection.
  clearCanvasSelection()
  const availableSources = connectionSources(store.allPrompts, store.connectionShots, generators.value)
  const restoredConnections: GeneratorConnection[] = []
  generatorConnections.value.forEach((wire) => {
    // Failed REST reads cannot prove a persisted source invalid. Keep the wire
    // dormant until its owning task can be loaded instead of deleting user data.
    if (!availableSources[wire.source] && !wire.source.startsWith('gen-') && Object.keys(unloadedJobKeys.value).length) { restoredConnections.push(wire); return }
    const valid = validateGeneratorConnection(wire, restoredConnections, generators.value, availableSources)
    if (valid.connection) restoredConnections.push(valid.connection)
  })
  generatorConnections.value = restoredConnections
  void generationTasks.restore()
  if (['canvas', 'shots', 'timeline'].includes(raw.activeView)) store.activeView = raw.activeView
  restoringCanvas.value = false
  buildGraph()
  if (recovered.duplicates) showToast(`已合并 ${recovered.duplicates} 条重复草稿，原画布已备份`, 3500)
  nextTick(() => {
    observeCanvas()
    const view = raw.viewport
    if (view && [view.x, view.y, view.zoom].every(Number.isFinite) && view.zoom >= MIN_CANVAS_ZOOM && view.zoom <= MAX_CANVAS_ZOOM) {
      viewport.value = { x: view.x, y: view.y, zoom: view.zoom }; canvasZoom.value = view.zoom
      if (setViewport) void setViewport(viewport.value)
      buildGraph()
    } else if (nodes.value.length) fitView({ maxZoom: 1.6, padding: 0.14 })
    persistCanvasState()
  })
})
watch(() => store.activeView, () => { closeGeneratorPreview(); nextTick(observeCanvas); persistCanvasState() })
onBeforeUnmount(() => {
  if (!props.projectId) persistCanvasState()
  canvasDisposed = true; streams.closeAll(); resetMagnet(); geometryObserver?.disconnect()
  clearTimeout(viewportSaveTimer); window.removeEventListener('keydown', onGlobalKeydown)
  window.removeEventListener('keydown', onPanKeydown, true)
  window.removeEventListener('keyup', releasePanKey); window.removeEventListener('blur', releasePanKey)
  window.removeEventListener('resize', measureCanvas)
  window.removeEventListener('pagehide', flushViewportSave); document.removeEventListener('visibilitychange', onPageVisibility)
  setApiProjectId('')
})
</script>

<template>
  <div ref="workbenchRoot" tabindex="-1" class="app-shell dark" :class="{ 'chrome-top-open': topbarOpen, 'chrome-left-open': leftRailOpen }">
    <div class="chrome-controls" aria-label="工作台面板控制"><button class="chrome-toggle" type="button" :aria-label="leftRailOpen ? '收起左侧栏' : '展开左侧栏'" :aria-expanded="leftRailOpen" title="左侧栏" @click="toggleChrome('left')"><Layers3 :size="18" /></button><button class="chrome-toggle" type="button" :aria-label="topbarOpen ? '收起顶栏' : '展开顶栏'" :aria-expanded="topbarOpen" title="顶栏与工作流" @click="toggleChrome('top')"><PanelRight :size="18" /></button></div>
    <header class="topbar" :inert="!topbarOpen" :aria-hidden="!topbarOpen"><div class="brand"><div class="brand-mark"><Sparkles :size="16" /></div><span>{{ brandName }}</span></div><div class="breadcrumb"><span>PROJECT</span><b>/</b><strong>{{ projectName || '多任务画布' }}</strong></div></header>
    <main class="workspace">
      <aside class="left-rail" :inert="!leftRailOpen" :aria-hidden="!leftRailOpen">
        <div class="rail-section"><span class="rail-label">WORKSPACE</span><button class="rail-item" :class="{ active: store.activeView === 'canvas' }" @click="store.selectView('canvas')"><Layers3 :size="16" />分镜拓扑</button><button class="rail-item" :class="{ active: store.activeView === 'shots' }" @click="store.selectView('shots')"><Table2 :size="16" />镜头列表 <b :title="`${store.shots.length} 个来源镜头 / ${generatedCount} 个生成片段`">{{ store.shots.length }} / {{ generatedCount }}</b></button><button class="rail-item" :class="{ active: store.activeView === 'timeline' }" @click="store.selectView('timeline')"><Clock3 :size="16" />时间轴</button></div>
        <div class="rail-section"><span class="rail-label">ANALYSIS</span><div class="analysis-state"><span class="pulse" /><div><strong>视觉解析</strong><small>{{ activeAnalysisLabel }}</small></div><b>{{ store.activeTask?.progress || 0 }}%</b></div><div class="stage"><span :class="{ 'stage-done': (store.activeTask?.progress || 0) >= 22 }"><Check :size="12" />素材预处理</span><span :class="{ 'stage-active': store.activeTask?.stage === 'analyzing' }"><Activity :size="12" />镜头与动作</span><span :class="{ 'stage-done': store.activeTask?.status === 'completed' }"><span class="stage-dot" />提示词生成</span></div></div>
        <div class="rail-bottom"><button class="rail-item" @click="showToast('Delete 删除选中节点或连线 · ⌘Z 撤回 · Esc 取消选择')"><Clipboard :size="16" />快捷键</button><button v-if="projectId" class="rail-item" @click="emit('exitProject')"><ChevronLeft :size="16" />返回项目列表</button></div>
      </aside>
      <section class="main-column">
        <div class="page-toolbar"><div class="toolbar-actions" aria-label="画布操作"><slot name="project-navigation" /><button class="cosmic-action undo-button" type="button" :disabled="!history.length" aria-label="撤回上一步画布操作" title="撤回操作" @click="undoCanvasAction"><Undo2 :size="17" /><span>撤回操作</span></button><button class="cosmic-action" type="button" aria-label="自动排版" title="自动排版" @click="autoLayout"><AlignJustify :size="17" /><span>自动排版</span></button><button class="cosmic-action" type="button" :disabled="!store.activeTask?.jobId" aria-label="全局画风" title="全局画风" @click="styleStudioShot = selected?.kind === 'shot' ? selected.shot?.id || '' : ''; styleStudioOpen = true"><Palette :size="17" /><span>全局画风</span></button><button class="cosmic-action cosmic-action-export" type="button" aria-label="导出 JSON" title="导出 JSON" @click="exportData"><Download :size="17" /><span>导出 JSON</span></button></div></div>
        <WorkflowRail v-if="topbarOpen" :task="store.activeTask" />
        <div v-if="store.activeView === 'canvas'" ref="canvasWrap" tabindex="-1" role="region" aria-label="分镜画布" class="canvas-wrap" :class="[`canvas-tool-${handMode ? 'hand' : 'select'}`, { 'canvas-compact-tools': canvasBounds.width < 980 }]" :style="{ '--selection-padding': `${12 / Math.max(MIN_CANVAS_ZOOM, viewport.zoom)}px`, '--preview-ui-scale': 1 / viewport.zoom }" @pointerdown.capture="beginCanvasPointer" @click.capture="captureCanvasClick" @dblclick="openCreateMenu" @pointermove.capture="handleMagneticPointer" @pointerleave="resetMagnet">
          <div v-if="restoringCanvas" class="canvas-restoring" role="status">正在恢复项目与节点…</div>
          <VueFlow v-model:nodes="nodes" v-model:edges="edges" :delete-key-code="null" :nodes-draggable="!handMode" :elements-selectable="!handMode" :node-drag-threshold="4" :selection-key-code="handMode ? 'Shift' : true" :pan-on-drag="handMode ? true : [1, 2]" :selection-mode="SelectionMode.Full" :zoom-on-scroll="false" :pan-on-scroll="false" :zoom-on-pinch="false" :zoom-activation-key-code="null" :prevent-scrolling="false" :default-viewport="viewport" :fit-view-on-init="false" :only-render-visible-elements="false" :zoom-on-double-click="false" :min-zoom="MIN_CANVAS_ZOOM" :max-zoom="MAX_CANVAS_ZOOM" :connection-radius="32" @selection-start="onMarqueeStart" @selection-end="onMarqueeEnd" @edge-click="handleEdgeClick" @node-click="handleNodeClick" @node-drag-start="onNodeDragStart" @node-drag-stop="onNodeDragStop" @nodes-change="onNodeDimensions" @viewport-change="onViewportChange" @pane-click="handlePaneClick" @connect="onConnect">
            <template #node-project="props"><ProjectNode v-bind="props" :snapshot-stale="staleSnapshots.has(props.data.jobId)" @sync-result="refreshTask(props.data.jobId, true)" @draft-change="(value) => { draftInputs[props.id] = value; persistCanvasState() }" @submit="(payload) => startJob(props.id, payload)" @cancel="cancelTask(props.id)" @resume="resumeTask(props.id)" @delete="deleteTask(props.id)" /></template>
            <template #node-scene="props"><SceneNode v-bind="props" @toggle="focusScene" /></template>
            <template #node-generator="props"><GeneratorNode v-bind="props" :preview-viewport="props.data.previewOpen ? viewport : undefined" @history-toggle="toggleGeneratorHistory" @select-version="selectGeneratedVersion" @preview-toggle="toggleGeneratorPreview" @preview-close="id => { if (id === generatorPreviewId) closeGeneratorPreview() }" @capture="captureGeneratorFrame" /></template>
            <template #edge-energy="props"><EnergyEdge v-bind="props" :flow-enabled="animatedEdgeIds.has(props.id)" /></template>
            <template #node-shot="props"><ShotNode v-bind="props" @select="select" /></template>
            <template #node-sequence="props"><GenerationSequenceNode v-bind="props" @select="select" @toggle="toggleSequence" /></template>
            <template #node-eventPrompt="props"><EventPromptNode v-bind="props" @select="select" @copy="copyText" /></template>
            <template #node-prompt="props"><PromptNode v-bind="props" @select="select" @copy="copyText" /></template>
            <Background pattern-color="#3f3f46" :gap="28" :size="1" /><TacticalMiniMap :nodes="nodes" :viewport="viewport" :active-keys="activeEffectKeys" :reduced="store.motionReduced" />
          </VueFlow>
          <div v-if="nodeActionAnchor && selectedNodeDetail" class="node-selection-actions nodrag nowheel nopan" role="toolbar" aria-label="选中节点操作" data-canvas-overlay
            :style="{ left: `${nodeActionAnchor.x}px`, top: `${nodeActionAnchor.y}px`, width: `${nodeActionAnchor.width}px` }" @pointerdown.stop @click.stop @dblclick.stop @wheel.stop>
            <button type="button" :aria-label="selectedNodeDetail.kind === 'task' ? '查看任务报告' : selectedNodeDetail.kind === 'prompt' ? '查看完整提示词' : '查看节点详情'" title="查看详情（I）" @click="openSelectedDetails"><Info :size="14" />{{ selectedNodeDetail.kind === 'task' ? '任务报告' : selectedNodeDetail.kind === 'prompt' ? '完整 Prompt' : '查看详情' }}</button>
            <button v-if="selectedPrompt && selectedNodeDetail.kind !== 'prompt'" type="button" aria-label="查看完整提示词" @click="openSelectedPrompt"><FileText :size="14" />Prompt</button>
          </div>
          <div class="canvas-tools nodrag nowheel nopan" role="toolbar" aria-label="画布手势与选择工具" @pointerdown.stop @dblclick.stop>
            <button type="button" :aria-pressed="selectionTool === 'select'" aria-label="框选工具" title="框选（V）：空白处拖动框选；⌘/Ctrl/Shift 单击增减选择" @click="selectionTool = 'select'"><MousePointer2 :size="16" /></button>
            <button type="button" :aria-pressed="selectionTool === 'hand'" aria-label="抓手工具" title="抓手（H）：拖动画布；空格或鼠标中键可临时平移" @click="selectionTool = 'hand'"><Hand :size="16" /></button>
            <i />
            <button type="button" aria-label="缩小画布" title="缩小；触控板捏合" @click="zoomCanvas(1 / 1.15)"><Minus :size="16" /></button>
            <span class="canvas-zoom" aria-label="当前画布缩放">{{ Math.round(viewport.zoom * 100) }}%</span>
            <button type="button" aria-label="放大画布" title="放大；触控板张开" @click="zoomCanvas(1.15)"><Plus :size="16" /></button>
            <button type="button" aria-label="显示全部节点" title="适应全部节点" @click="fitView({ maxZoom: 1.6, padding: .14, duration: store.motionReduced ? 0 : 240 })"><Scan :size="16" /></button>
            <template v-if="selectedNodeIds.size > 1"><i /><span class="canvas-selection-count" role="status">{{ selectedNodeIds.size }} 个已选</span><button type="button" aria-label="删除选中节点" title="删除选中节点；⌘Z 整组撤回" @click="deleteSelectedNodes"><Trash2 :size="16" /></button></template>
            <button v-if="canvasHiddenNodeIds.length" type="button" class="canvas-restore-hidden" @click="restoreHiddenNodes">恢复隐藏 {{ canvasHiddenNodeIds.length }}</button>
          </div>
          <GeneratorInlinePanel v-if="selectedGenerator && selectedGeneratorResolved && selectedGeneratorAnchor"
            :key="selectedGenerator.id" :generator="selectedGenerator" :resolved="selectedGeneratorResolved" :anchor="selectedGeneratorAnchor"
            :connections="generatorConnections.filter(wire => wire.target === selectedGenerator?.id)"
            :task="generationTasks.jobs.value[selectedGenerator.id]" :capabilities="generationTasks.capabilities.value"
            :pending-recovery="generationTasks.canRecoverSubmission(selectedGenerator.id)"
            :busy="generationTasks.busy.value[selectedGenerator.id]" :stale="generationTasks.isStale(selectedGenerator.id)" :task-error="generationTasks.errors.value[selectedGenerator.id]"
            @close="handlePaneClick" @prompt-change="value => changeGeneratorPrompt(selectedGenerator!.id, value)"
            @settings-change="value => changeGeneratorSettings(selectedGenerator!.id, value)"
            @reference-change="value => generatorEdits.reference(selectedGenerator!.id, value)" @remove-reference="id => removeReference(selectedGenerator!.id, id)"
            @import-image="file => importGeneratorImage(selectedGenerator!.id, file)" @remove-image="removeGeneratorImage(selectedGenerator.id)"
            @disconnect="port => disconnectGenerator(selectedGenerator!.id, port)" @remove-node="deleteGenerator(selectedGenerator.id)"
            @submit-generation="generationTasks.submit(selectedGenerator.id)" @cancel-generation="generationTasks.cancel(selectedGenerator.id)" @retry-generation="generationTasks.retry(selectedGenerator.id)"
            @analyze-style="openImageStyle(selectedGenerator)" />
          <div v-if="!store.tasks.length && !generators.length" class="canvas-empty"><Sparkles :size="24" /><strong>双击空白画布</strong><span>创建输入节点或生成节点</span></div>
          <button v-if="focusedScene" class="movie-breadcrumb" type="button" @click="showWholeMovie"><ChevronLeft :size="14" /> 返回全片地图</button>
          <div v-if="createMenu" class="create-node-menu" :style="{ left: `${createMenu.screen.x}px`, top: `${createMenu.screen.y}px` }" @dblclick.stop>
            <span>创建节点</span><button type="button" @click="createNode('video')"><Film :size="15" /><div><strong>视频输入</strong><small>上传 MP4 并分析画面</small></div></button><button type="button" @click="createNode('script')"><Type :size="15" /><div><strong>剧本输入</strong><small>粘贴文本并拆分镜头</small></div></button><div class="create-menu-divider" /><button type="button" @click="createNode('image-generator')"><ImageIcon :size="15" /><div><strong>图片生成</strong><small>连接 Prompt · 导入图片</small></div></button><button type="button" @click="createNode('video-generator')"><VideoIcon :size="15" /><div><strong>视频生成</strong><small>连接首帧与视频 Prompt</small></div></button>
          </div>
        </div>
        <div v-else-if="store.activeView === 'shots'" class="list-view">
          <DeliveryQualityPanel :quality="store.activeTask?.deliveryQuality" :status="store.activeTask?.status || ''" />
          <div class="list-toolbar"><div class="search"><Search :size="15" /><input v-model="store.query" placeholder="搜索镜头动作..." /></div><select v-model="listBasis" class="filter-button" aria-label="列表内容"><option value="sequences">生成片段</option><option value="source">来源镜头</option></select><select v-model="store.filter" class="filter-button" aria-label="结果状态筛选"><option value="all">全部状态</option><option value="ready">可用</option><option value="warning">需复核</option><option value="failed">失败</option><option value="stale">已过期</option><option value="processing">处理中</option></select></div>
          <div class="view-detail-actions"><button class="outline-button" type="button" :disabled="!selectedNodeDetail" aria-label="查看选中项详情" @click="openSelectedDetails"><Info :size="14" />查看详情 <kbd>I</kbd></button><button v-if="selectedPrompt" class="outline-button" type="button" @click="openSelectedPrompt"><FileText :size="14" />完整 Prompt</button></div>
          <p class="list-counts">{{ store.shots.length }} 个来源镜头 / {{ generatedCount }} 个生成片段 · 当前显示 {{ listRows.length }} 项；剧本时间为建议表演时间</p>
          <div class="shot-table"><div class="table-head"><span>任务 / 镜号</span><span>来源或表演时间</span><span>动作摘要</span><span>景别 / 来源镜头</span><span>状态</span></div><button v-for="{ shot, task } in listRows" :key="shot.id" class="table-row" @click="select(shot.id)"><span class="table-shot"><img v-if="shot.thumb" :src="shot.thumb" alt="镜头缩略图" loading="lazy" decoding="async" /><span><small class="list-task-label">{{ task.videoFilename || '文字剧本' }} · {{ task.jobId }}</small><b>{{ shot.generationSequence ? 'SEQ' : 'SH' }} {{ String(shot.number).padStart(2, '0') }}</b></span></span><span class="mono">{{ shot.time }}</span><span>{{ shot.action }}</span><span>{{ shot.generationSequence ? `${shot.generationSequence.source_shot_ids.length} 个来源镜头 · ${shot.duration} 建议` : `${shot.shotSize} · ${shot.lens}` }}</span><StatusPill :status="shot.status" /></button></div>
        </div>
        <TimelineView v-else :tasks="store.tasks" :selected-id="store.selectedId" @select="select" @details="id => openNodeDetails(id)" />
      </section>
    </main>
    <NodeDetailsHost v-if="details.target.value && details.resolved.value"
      :target="details.target.value" :task="details.resolved.value.task" :shot="details.resolved.value.shot"
      :sequence="details.resolved.value.sequence" :prompt="details.resolved.value.prompt" :scene="details.resolved.value.scene"
      :raw-shots="details.resolved.value.rawShots" :metadata="details.resolved.value.task.sequenceMetadata" :initial-tab="details.tab.value"
      @close="closeDetails" @source-shot="inspectSourceShot" @sequence="returnToDetailSequence" @copy="copyText" @updated="refreshTask"
      @style-effects="openDetailStyle" @open-event-agent="openEventAgent"
      @open-prompt="value => openNodeDetails(value.promptId, { taskKey: value.taskKey })" />
    <EventAgentStudio v-if="eventAgentOpen && eventAgentTask" :job-id="eventAgentTask.jobId" :job-status="eventAgentTask.status" :input-type="eventAgentTask.inputType" :job-label="eventAgentTask.videoFilename" :source-shot-ids="eventAgentTask.shots.map(shot => shot.id)" @close="eventAgentOpen = false; focusWorkbench()" />
    <DirectStyleStudio v-if="styleStudioOpen && (styleImageBlob || store.activeTask?.styleStrategy === 'qwen-direct-v3' || store.activeTask?.directStyleAnalysisId)" :job-id="styleImageBlob ? undefined : store.activeTask?.jobId || undefined" :image-blob="styleImageBlob" :image-asset-id="styleImageAssetId" :targets="styleTargets" @close="closeStyleStudio" @applied="onStyleApplied" />
    <VisualBibleStudio v-else-if="styleStudioOpen && store.activeTask?.jobId && store.activeTask.styleStrategy === 'vector-v2'" :job-id="store.activeTask.jobId" :input-type="store.activeTask.inputType" :shot-ids="store.activeTask.shots.map((shot) => shot.id)" :scene-ids="store.activeTask.sceneGroups?.map((scene) => scene.id) || []" :selected-shot-id="styleStudioShot" @close="styleStudioOpen = false" @applied="onStyleApplied" />
    <StyleStudio v-else-if="styleStudioOpen && store.activeTask?.jobId" :job-id="store.activeTask.jobId" :input-type="store.activeTask.inputType" :shot-ids="store.activeTask.shots.map((shot) => shot.id)" :selected-shot-id="styleStudioShot" @close="styleStudioOpen = false" @applied="onStyleApplied" />
    <div v-if="toast" class="toast"><Check :size="15" />{{ toast }}</div>
  </div>
</template>

<style scoped>
.list-counts{font-size:11px;color:#8c9aa4;margin:0 0 12px}.list-task-label{display:block;max-width:140px;overflow:hidden;text-overflow:ellipsis;font-size:8px;line-height:1.7;color:#8c9aa4}.workflow-review{color:#c4a071}
.canvas-restoring{position:absolute;inset:0;z-index:80;display:grid;place-content:center;background:#18181be8;color:#a6bfcd;font-size:13px}
.canvas-wrap:focus,.app-shell:focus{outline:none}
.node-selection-actions{position:absolute;z-index:9;display:flex;justify-content:center;gap:4px;padding:4px;border:1px solid #58728666;border-radius:9px;background:#1b2632f5;box-shadow:0 5px 18px #0003;color:#bcd6e7}
.node-selection-actions button,.view-detail-actions button{display:flex;align-items:center;justify-content:center;gap:6px;padding:5px 8px;border:0;border-radius:6px;background:transparent;color:inherit;white-space:nowrap;font-size:11px;cursor:pointer}.node-selection-actions button:hover{background:#3b5e7244}.node-selection-actions button:focus-visible{outline:2px solid #83d8ed;outline-offset:1px}
.view-detail-actions{display:flex;gap:8px;margin-bottom:10px;color:#bcd6e7}.view-detail-actions button{border:1px solid #58728666}.view-detail-actions button:disabled{opacity:.45;cursor:default}.view-detail-actions kbd{font:10px monospace;color:#7fa4bd}
.canvas-tools{position:absolute;left:50%;bottom:16px;transform:translateX(-50%);z-index:8;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:3px;padding:5px;border:1px solid #51687b66;border-radius:12px;background:#18212bed;color:#bed1df;box-shadow:0 6px 24px #0004;max-width:calc(100% - 32px)}
.canvas-tools button{display:grid;place-items:center;flex-shrink:0;width:32px;height:32px;border:0;border-radius:7px;background:transparent;color:inherit;cursor:pointer}.canvas-tools button:hover{background:#334656}.canvas-tools button[aria-pressed=true]{color:#b5edff;background:#315061}.canvas-tools button:focus-visible{outline:2px solid #75d7ee;outline-offset:1px}.canvas-tools i{height:18px;width:1px;background:#60788b55;margin:0 3px}.canvas-zoom{min-width:38px;text-align:center;font:10px 'DM Mono',monospace;user-select:none}.canvas-selection-count{white-space:nowrap;font-size:10px}.canvas-tools .canvas-restore-hidden{width:auto;padding:0 8px;white-space:nowrap;font-size:10px}
.canvas-tool-select :deep(.vue-flow__pane){cursor:crosshair}.canvas-tool-hand :deep(.vue-flow__pane){cursor:grab}.canvas-tool-hand :deep(.vue-flow__pane.dragging){cursor:grabbing}
.canvas-tool-hand :deep(.vue-flow__node),.canvas-tool-hand :deep(.vue-flow__node *),.canvas-tool-hand :deep(.vue-flow__nodesselection),.canvas-tool-hand :deep(.vue-flow__nodesselection *){pointer-events:none!important}
.canvas-compact-tools .canvas-tools{max-width:calc(100% - 120px)}
.canvas-tool-hand :deep(.generator-preview-open :is(button, input, a[href])){pointer-events:auto!important}
:deep(.vue-flow__selection){background:#6acde819;border:1px solid #75d7ee;border-radius:5px}
:deep(.vue-flow__nodesselection-rect){box-sizing:content-box;padding:var(--selection-padding,12px);margin-left:calc(-1 * var(--selection-padding,12px));margin-top:calc(-1 * var(--selection-padding,12px));border:0;outline:calc(var(--selection-padding,12px) / 12) solid #75d7ee88;border-radius:calc(var(--selection-padding,12px) * .75);background:#6acde808}
:deep(.vue-flow__node.selected .generation-sequence-node),:deep(.vue-flow__node.selected .scene-deck-node){outline:2px solid #75d7ee;outline-offset:4px}
</style>
