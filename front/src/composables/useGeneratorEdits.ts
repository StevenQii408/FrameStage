import type { Ref } from 'vue'
import { connectionSources, removeGeneratorConnection, validateGeneratorConnection,
  type ConnectionCandidate, type GeneratorConnection, type GeneratorDraft, type ResolvedGenerator } from '../generatorGraph'
import type { GenerationSettings } from '../api/generations'
import { isReferenceAsset } from '../generatorVersions'

interface EditContext {
  generators: Ref<GeneratorDraft[]>
  connections: Ref<GeneratorConnection[]>
  sources: () => ReturnType<typeof connectionSources>
  snapshot: () => void
  changed: () => void
  notify: (message: string) => void
  beforeRemove: (id: string) => void
  resolve?: (id: string) => ResolvedGenerator
}

/** Local editing is independent from paid job execution and canvas rendering. */
export function useGeneratorEdits(context: EditContext) {
  const { generators, connections, snapshot, changed } = context
  function connect(candidate: ConnectionCandidate) {
    const checked = validateGeneratorConnection(candidate, connections.value, generators.value, context.sources())
    if (!checked.ok || !checked.connection) { context.notify(checked.reason); return null }
    snapshot()
    connections.value.push(checked.connection)
    changed()
    return checked.connection
  }
  function disconnect(id: string, port: 'prompt-in' | 'image-in') {
    if (!connections.value.some(wire => wire.target === id && wire.targetHandle === port)) return
    snapshot()
    connections.value = connections.value.filter(wire => wire.target !== id || wire.targetHandle !== port)
    changed()
  }
  function removeConnection(id: string) {
    if (!connections.value.some(wire => wire.id === id)) return
    snapshot()
    connections.value = removeGeneratorConnection(connections.value, id)
    changed()
  }
  function remove(id: string) {
    if (!generators.value.some(item => item.id === id)) return
    snapshot()
    context.beforeRemove(id)
    generators.value = generators.value.filter(item => item.id !== id)
    connections.value = connections.value.filter(wire => wire.source !== id && wire.target !== id)
    changed()
  }
  function prompt(id: string, text: string) {
    const item = generators.value.find(node => node.id === id)
    if (!item) return
    if (isReferenceAsset(item)) { context.notify('上传图片是只读参考资产，不能设置生成 Prompt'); return }
    const resolved = context.resolve?.(id)
    if (resolved?.promptSourceId && resolved.sourcePrompt !== undefined) {
      item.connectedPromptOverride = { sourceId: resolved.promptSourceId, sourceText: resolved.sourcePrompt, text: text.slice(0, 10000) }
    } else item.manualPrompt = text.slice(0, 10000)
    changed()
  }
  function settings(id: string, value: GenerationSettings) {
    const item = generators.value.find(node => node.id === id)
    if (!item) return
    if (isReferenceAsset(item)) { context.notify('参考图片保持原始比例，不能修改生成参数'); return }
    snapshot()
    item.generationSettings = value
    changed()
  }
  function reference(id: string, change: { id: string; selected?: boolean; firstFrame?: boolean }) {
    const item = generators.value.find(node => node.id === id)
    if (!item) return
    snapshot()
    if (change.selected !== undefined) item.referenceSelections = { ...item.referenceSelections, [change.id]: change.selected }
    if (change.firstFrame) { item.firstFrameReferenceId = change.id; item.referenceSelections = { ...item.referenceSelections, [change.id]: true } }
    changed()
  }
  return { connect, disconnect, removeConnection, remove, prompt, settings, reference }
}
