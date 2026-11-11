import { onBeforeUnmount, ref, type Ref } from 'vue'
import { generatorAssetIds, generatorReferenceId, type GeneratorDraft } from '../generatorGraph'
import { imageDimensions, loadLocalImage, saveLocalImage, validateLocalImage } from '../generatorStorage'
import { isReferenceAsset } from '../generatorVersions'

/** Browser-only references stay separate from backend generated artifacts. */
export function useGeneratorAssets(generators: Ref<GeneratorDraft[]>, changed: () => void,
  snapshot: () => void, notify: (message: string) => void) {
  const assetUrls = ref<Record<string, string>>({})
  let disposed = false
  let hydrationVersion = 0
  async function hydrateAssets() {
    const version = ++hydrationVersion
    const used = new Set(generators.value.flatMap(generatorAssetIds))
    const next = { ...assetUrls.value }
    const created: string[] = []
    for (const id of Object.keys(next)) if (!used.has(id)) delete next[id]
    for (const id of used) if (!next[id]) {
      try {
        const blob = await loadLocalImage(id)
        if (disposed || version !== hydrationVersion) break
        if (blob && generators.value.some(item => generatorAssetIds(item).includes(id))) {
          next[id] = URL.createObjectURL(blob)
          created.push(next[id])
        }
      } catch { notify('本地图片读取失败，请重新导入') }
    }
    if (disposed || version !== hydrationVersion) { created.forEach(url => URL.revokeObjectURL(url)); return }
    for (const [id, url] of Object.entries(assetUrls.value)) if (next[id] !== url) URL.revokeObjectURL(url)
    assetUrls.value = next
    changed()
  }
  async function importGeneratorImage(id: string, file: File, options: { asReference?: boolean } = {}) {
    const item = generators.value.find(node => node.id === id)
    if (!item) return
    const assetImport = () => item.kind === 'image'
      && (isReferenceAsset(item) || !options.asReference && !item.generationTask && !item.pendingSubmission && !item.versions?.length)
    if (!assetImport() && generatorAssetIds(item).length >= 8) { notify('每个节点最多保留八张本地参考图片'); return }
    const error = validateLocalImage(file)
    if (error) { notify(error); return }
    try {
      const dimensions = await imageDimensions(file)
      if (!dimensions.width || !dimensions.height) throw new Error('无法读取图片尺寸')
      const assetId = 'image-' + crypto.randomUUID()
      await saveLocalImage(assetId, file)
      if (disposed || !generators.value.includes(item)) return
      // A paid submission may have started while the image decoder was open.
      const importAsAsset = assetImport()
      if (!importAsAsset && generatorAssetIds(item).length >= 8) { notify('每个节点最多保留八张本地参考图片'); return }
      snapshot()
      item.referenceImages = importAsAsset ? [{ assetId, ...dimensions }] : [...(item.referenceImages || []), { assetId, ...dimensions }]
      if (importAsAsset || !item.imageAssetId) {
        item.imageAssetId = assetId
        item.imageWidth = dimensions.width
        item.imageHeight = dimensions.height
      }
      item.nodeMode = importAsAsset ? 'reference' : 'generator'
      assetUrls.value = { ...assetUrls.value, [assetId]: URL.createObjectURL(file) }
      changed()
      await hydrateAssets()
      notify(importAsAsset ? '参考图片资产已保存，保持原图比例，不参与生成' : '本地参考图已保存')
    } catch (error) { notify(error instanceof Error ? error.message : '图片导入失败') }
  }
  async function addReferenceImage(file: File, position: { x: number; y: number }): Promise<GeneratorDraft | null> {
    const error = validateLocalImage(file)
    if (error) { notify(error); return null }
    try {
      const dimensions = await imageDimensions(file)
      if (!dimensions.width || !dimensions.height) throw new Error('无法读取截帧图片尺寸')
      const assetId = 'image-' + crypto.randomUUID()
      await saveLocalImage(assetId, file)
      if (disposed) return null
      const item: GeneratorDraft = { id: 'gen-' + crypto.randomUUID(), kind: 'image', nodeMode: 'reference',
        position: { ...position }, manualPrompt: '', imageAssetId: assetId,
        imageWidth: dimensions.width, imageHeight: dimensions.height,
        referenceImages: [{ assetId, ...dimensions }] }
      snapshot()
      generators.value.push(item)
      assetUrls.value = { ...assetUrls.value, [assetId]: URL.createObjectURL(file) }
      changed()
      return item
    } catch (error) { notify(error instanceof Error ? error.message : '截帧图片保存失败'); return null }
  }
  function removeGeneratorImage(id: string) {
    const item = generators.value.find(node => node.id === id)
    if (!item?.imageAssetId) return
    snapshot()
    item.imageAssetId = null
    item.imageWidth = item.imageHeight = 0
    item.referenceImages = []
    changed()
    void hydrateAssets()
  }
  function removeGeneratorReference(id: string, referenceId: string) {
    const item = generators.value.find(node => node.id === id)
    if (!item) return
    const assetId = generatorAssetIds(item).find(asset => generatorReferenceId('local:' + asset) === referenceId)
    if (!assetId) return
    snapshot()
    item.referenceImages = (item.referenceImages || []).filter(image => image.assetId !== assetId)
    if (item.imageAssetId === assetId) {
      const next = item.referenceImages[0]
      item.imageAssetId = next?.assetId || null
      item.imageWidth = next?.width || 0
      item.imageHeight = next?.height || 0
    }
    changed()
    void hydrateAssets()
  }
  onBeforeUnmount(() => { disposed = true; hydrationVersion++; Object.values(assetUrls.value).forEach(url => URL.revokeObjectURL(url)) })
  const importGeneratorReference = (id: string, file: File) => importGeneratorImage(id, file, { asReference: true })
  return { assetUrls, hydrateAssets, importGeneratorImage, importGeneratorReference, removeGeneratorImage, removeGeneratorReference, addReferenceImage }
}
