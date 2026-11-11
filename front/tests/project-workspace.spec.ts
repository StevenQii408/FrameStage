import { ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ProjectWorkspace from '../src/views/ProjectWorkspace.vue'
import { useProjectCanvas } from '../src/composables/useProjectCanvas'

const router = vi.hoisted(() => ({ push: vi.fn(), leave: vi.fn(), update: vi.fn(), prepareLeave: vi.fn(() => true) }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: router.push }), onBeforeRouteLeave: router.leave, onBeforeRouteUpdate: router.update }))
vi.mock('../src/composables/useProjectCanvas', () => ({ useProjectCanvas: vi.fn() }))
vi.mock('../src/App.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return { __esModule: true, default: defineComponent({ name: 'WorkbenchStub', inheritAttrs: false,
    props: ['projectId', 'projectName', 'initialCanvas'], emits: ['canvasChange', 'exitProject'],
    setup(_props, { attrs, expose, slots }) {
      expose({ prepareLeave: router.prepareLeave })
      return () => h('div', { class: 'workbench-stub', ...attrs }, [h('div', { class: 'toolbar-actions' }, [slots['project-navigation']?.(), h('button', { 'aria-label': '示例画布操作' }, '操作')])])
    },
  }) }
})

function state() {
  return { project: ref({ id: 'project-a', name: '长片项目', job_ids: ['job-a'] }), initialState: ref({ viewport: { x: 10, y: 20, zoom: .8 } }),
    ready: ref(true), error: ref(''), conflict: ref(false), unavailable: ref(false), status: ref<'saved' | 'saving' | 'local' | 'memory' | 'conflict'>('saved'),
    load: vi.fn(async () => undefined), update: vi.fn(), flush: vi.fn(async () => true), downloadLocal: vi.fn(), reloadServer: vi.fn(async () => undefined) }
}
beforeEach(() => { router.prepareLeave.mockReturnValue(true) })
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('project workspace toolbar navigation', () => {
  it('places a single flow navigation group in the Workbench slot before canvas actions', async () => {
    const canvas = state()
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    expect(canvas.load).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.project-nav')).toHaveLength(1)
    expect(wrapper.find('.project-workspace > .project-nav').exists()).toBe(false)
    expect(wrapper.get('.toolbar-actions').element.firstElementChild).toBe(wrapper.get('.project-nav').element)
    expect(wrapper.get('.project-navigation-name').text()).toBe('长片项目')
    expect(wrapper.get('.project-navigation-emblem').attributes('aria-hidden')).toBe('true')
    expect(wrapper.get('.project-save-dot').attributes('aria-hidden')).toBe('true')
    const app = wrapper.getComponent({ name: 'WorkbenchStub' })
    expect(app.props()).toMatchObject({ projectId: 'project-a', projectName: '长片项目', initialCanvas: { backendJobIds: ['job-a'] } })
    await wrapper.get('.project-nav [aria-label="返回项目列表"]').trigger('click')
    expect(router.push).toHaveBeenCalledWith('/projects')
    await wrapper.get('.project-save-state').trigger('click')
    expect(canvas.flush).toHaveBeenCalledOnce()
    app.vm.$emit('canvasChange', { viewport: { x: 1, y: 2, zoom: 1 } })
    expect(canvas.update).toHaveBeenCalledWith({ viewport: { x: 1, y: 2, zoom: 1 } })
    wrapper.unmount()
  })
  it('retains saving, offline retry and conflict recovery without duplicating navigation', async () => {
    const canvas = state()
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    canvas.status.value = 'saving'
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.project-save-state').attributes('disabled')).toBeDefined()
    expect(wrapper.get('.project-save-state').attributes('aria-busy')).toBe('true')
    expect(wrapper.get('.project-nav').attributes('data-save-state')).toBe('saving')
    canvas.status.value = 'local'; canvas.error.value = '保存请求失败'
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.project-save-state').text()).toContain('仅保存在本机')
    expect(wrapper.get('.project-save-state').attributes('title')).toBe('保存请求失败')
    await wrapper.get('.project-save-state').trigger('click')
    expect(canvas.flush).toHaveBeenCalledOnce()
    canvas.status.value = 'conflict'; canvas.conflict.value = true
    await wrapper.vm.$nextTick()
    const dialog = wrapper.get('[aria-label="画布版本冲突"]')
    expect(dialog.text()).toContain('本机草稿仍然保留')
    await dialog.findAll('button')[0].trigger('click')
    expect(canvas.downloadLocal).toHaveBeenCalledOnce()
    await dialog.findAll('button')[1].trigger('click')
    await flushPromises()
    expect(canvas.reloadServer).toHaveBeenCalledOnce()
    expect(wrapper.findAll('.project-nav')).toHaveLength(1)
    wrapper.unmount()
  })
  it('offers a current-draft download and avoids local-save claims when storage is full', async () => {
    const canvas = state()
    canvas.status.value = 'memory'
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    expect(wrapper.get('.project-save-state').text()).toContain('仅在当前窗口')
    await wrapper.get('[aria-label="下载当前草稿"]').trigger('click')
    expect(canvas.downloadLocal).toHaveBeenCalledOnce()
    canvas.unavailable.value = true
    await wrapper.vm.$nextTick()
    expect(wrapper.get('[role="dialog"]').text()).toContain('最新草稿仅在当前窗口')
    wrapper.unmount()
  })

  it('preserves route leave/update guards and deleted-project inert behavior', async () => {
    const canvas = state()
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    const beforeLeave = router.leave.mock.calls[0][0]
    const beforeUpdate = router.update.mock.calls[0][0]
    router.prepareLeave.mockReturnValue(false)
    expect(await beforeLeave()).toBe(false)
    expect(canvas.flush).not.toHaveBeenCalled()
    router.prepareLeave.mockReturnValue(true)
    canvas.flush.mockResolvedValueOnce(false)
    expect(await beforeUpdate()).toBe(false)
    expect(canvas.flush).toHaveBeenCalledOnce()
    canvas.unavailable.value = true
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.workbench-stub').attributes('inert')).toBeDefined()
    expect(wrapper.get('[aria-label="项目已删除"]').text()).toContain('本机草稿已保留')
    wrapper.unmount()
  })
  it('does not show navigation until a canvas is ready and retains read retry', async () => {
    const canvas = state(); canvas.ready.value = false; canvas.error.value = '读取失败'
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    expect(wrapper.find('.project-nav').exists()).toBe(false)
    expect(wrapper.find('.workbench-stub').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toBe('读取失败')
    await wrapper.get('.project-loading').findAll('button')[1].trigger('click')
    expect(canvas.load).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })
  it('flashes once after a real saving-to-saved transition and leaves historical saved state static', async () => {
    const canvas = state()
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    await flushPromises()
    const nav = wrapper.get('.project-nav')
    expect(nav.classes()).not.toContain('project-nav-saved-flash')
    canvas.status.value = 'saving'
    await wrapper.vm.$nextTick()
    canvas.status.value = 'saved'
    await wrapper.vm.$nextTick()
    expect(nav.classes()).toContain('project-nav-saved-flash')
    // Scoped styles suffix animation names in production.
    await nav.trigger('animationend', { animationName: 'project-nav-saved-scope123' })
    expect(nav.classes()).not.toContain('project-nav-saved-flash')
    canvas.project.value.name = '同一已保存项目'
    await wrapper.vm.$nextTick()
    expect(nav.classes()).not.toContain('project-nav-saved-flash')
    wrapper.unmount()
  })
  it('stops navigation animation when hidden or reduced-motion is requested and cleans up listeners', async () => {
    const originalVisibility = Object.getOwnPropertyDescriptor(document, 'visibilityState')
    let visibility = 'visible'
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility })
    const media = { matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }
    vi.stubGlobal('matchMedia', vi.fn(() => media))
    const removeListener = vi.spyOn(document, 'removeEventListener')
    const canvas = state()
    vi.mocked(useProjectCanvas).mockReturnValue(canvas as unknown as ReturnType<typeof useProjectCanvas>)
    const wrapper = mount(ProjectWorkspace, { props: { projectId: 'project-a' } })
    try {
      await flushPromises()
      const nav = wrapper.get('.project-nav')
      expect(nav.classes()).toContain('project-nav-animated')
      canvas.status.value = 'saving'
      await wrapper.vm.$nextTick()
      visibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange'))
      await wrapper.vm.$nextTick()
      expect(nav.classes()).not.toContain('project-nav-animated')
      canvas.status.value = 'saved'
      await wrapper.vm.$nextTick()
      visibility = 'visible'; document.dispatchEvent(new Event('visibilitychange'))
      await wrapper.vm.$nextTick()
      expect(nav.classes()).not.toContain('project-nav-saved-flash')
      const changed = media.addEventListener.mock.calls[0][1]
      changed({ matches: true })
      await wrapper.vm.$nextTick()
      expect(nav.classes()).not.toContain('project-nav-animated')
      canvas.status.value = 'saving'
      await wrapper.vm.$nextTick()
      canvas.status.value = 'saved'
      await wrapper.vm.$nextTick()
      expect(nav.classes()).not.toContain('project-nav-saved-flash')
      wrapper.unmount()
      expect(media.removeEventListener).toHaveBeenCalledWith('change', changed)
      expect(removeListener.mock.calls.some(call => call[0] === 'visibilitychange')).toBe(true)
    } finally {
      wrapper.unmount()
      if (originalVisibility) Object.defineProperty(document, 'visibilityState', originalVisibility)
      else delete (document as unknown as Record<string, unknown>).visibilityState
    }
  })
})
