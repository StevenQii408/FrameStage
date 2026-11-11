export function isBlankCanvasDoubleClick(target: Element): boolean {
  return Boolean(target.closest('.vue-flow__pane') && !target.closest('.vue-flow__node, .generator-preview-surface, .create-node-menu'))
}
