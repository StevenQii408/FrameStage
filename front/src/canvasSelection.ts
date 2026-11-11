export interface CanvasDeletionRoot {
  id: string
  /** Complete system subtree, including children currently collapsed/hidden. */
  descendantIds: Iterable<string>
}

export interface CanvasDeletionContext {
  roots: readonly CanvasDeletionRoot[]
  generatorIds: Iterable<string>
  /** Only currently visible/selectable nodes; stale selections are ignored. */
  nodeIds: Iterable<string>
}

export interface CanvasDeletionPlan {
  taskIds: string[]
  generatorIds: string[]
  /** Independently selected derived nodes are hidden, never deleted upstream. */
  hiddenNodeIds: string[]
  /** Full closure for local positions, selection and incident-wire cleanup. */
  removedNodeIds: string[]
}

/**
 * Plan one reversible canvas transaction without mutating data or invoking APIs.
 * A root owns only its analysis subtree; outgoing wires do not confer ownership
 * of manually created generation nodes. Only explicit selection removes those.
 */
export function planCanvasDeletion(selectedIds: Iterable<string>, context: CanvasDeletionContext): CanvasDeletionPlan {
  const visible = new Set(context.nodeIds)
  const selected = new Set(Array.from(selectedIds).filter(id => visible.has(id)))
  const generators = new Set(context.generatorIds)
  const roots = new Map(context.roots.map(root => [root.id, root]))
  const taskIds = [...selected].filter(id => roots.has(id) && !generators.has(id))
  const generatorIds = [...selected].filter(id => generators.has(id))
  const covered = new Set(taskIds)

  for (const id of taskIds) {
    for (const child of roots.get(id)!.descendantIds) {
      // Protect user-owned generators even if an upstream closure is too broad.
      if (!generators.has(child)) covered.add(child)
    }
  }

  const hiddenNodeIds = [...selected].filter(id => !generators.has(id) && !covered.has(id))
  return {
    taskIds,
    generatorIds,
    hiddenNodeIds,
    removedNodeIds: [...new Set([...covered, ...generatorIds, ...hiddenNodeIds])],
  }
}
