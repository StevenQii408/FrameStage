export function generatorPanelAnchor(node: { left: number; top: number; width: number; height: number }, screen: { width: number; height: number }) {
  const width = Math.max(280, Math.min(720, screen.width - 24))
  const below = node.top + node.height + 14
  // The editor can shrink its text area near the bottom. Do not pull it up
  // across the node, where it would hide the generation percentage/media.
  const y = screen.height - below - 12 >= 180 ? below
    : node.top >= 356 ? node.top - 344
    : Math.max(12, Math.min(screen.height - 330, below))
  return {
    x: Math.max(12, Math.min(screen.width - width - 12, node.left + node.width / 2 - width / 2)),
    y: Math.max(12, y),
    width,
  }
}
