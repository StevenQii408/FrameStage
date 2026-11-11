/** Native mouse/keyboard regression against an explicitly started, isolated QA server. */
import { spawn } from 'node:child_process'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { chromeExecutable, temporaryDirectory } from './browser-environment.mjs'
import assert from 'node:assert/strict'

const args = Object.fromEntries(process.argv.slice(2).flatMap((value, index, all) => value.startsWith('--') ? [[value.slice(2), all[index + 1]]] : []))
assert.equal(args['qa-only'], 'yes', 'Pass --qa-only yes and explicit isolated --url/--api addresses')
assert.ok(args.url && args.api, 'An isolated frontend and backend URL are required')
const appUrl = args.url.replace(/\/$/, '')
const apiUrl = args.api.replace(/\/$/, '')
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(apiUrl).hostname), 'QA must run on loopback')
assert.notEqual(new URL(apiUrl).port, '8000', 'Do not run project-writing QA against the normal backend port')
const output = args.output || await temporaryDirectory('frameforge-canvas-interactions-')
await mkdir(output, { recursive: true })
const profile = await mkdtemp(output + '/chrome-')
const chrome = spawn(await chromeExecutable(args.chrome), [
  '--headless=new', '--disable-gpu', '--no-first-run', '--disable-background-networking', '--disable-extensions',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
], { stdio: 'ignore' })
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
const report = { checks: {}, errors: [], screenshots: [] }
const rectExpression = `element => { const r = element.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, right: r.right, bottom: r.bottom } }`
let socket
try {
  let port
  for (let n = 0; n < 100; n++) {
    try { port = (await readFile(profile + '/DevToolsActivePort', 'utf8')).split('\n')[0]; break } catch { await delay(100) }
  }
  assert.ok(port, 'Isolated Chrome starts')
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  socket = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl)
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }) })
  let counter = 0
  const pending = new Map()
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data)
    if (message.id) {
      const request = pending.get(message.id)
      pending.delete(message.id)
      if (message.error) request?.reject(new Error(JSON.stringify(message.error)))
      else request?.resolve(message.result)
    } else if (message.method === 'Runtime.exceptionThrown') {
      report.errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text)
    }
  })
  function send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++counter
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('CDP timeout: ' + method)) }, 15000)
      pending.set(id, { resolve: value => { clearTimeout(timeout); resolve(value) }, reject: error => { clearTimeout(timeout); reject(error) } })
      socket.send(JSON.stringify({ id, method, params }))
    })
  }
  async function evaluate(expression) {
    const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
    return result.result.value
  }
  async function until(expression) {
    for (let n = 0; n < 160; n++) { if (await evaluate(expression)) return; await delay(100) }
    throw new Error('Browser condition not met: ' + expression)
  }
  async function screenshot(name) {
    await delay(180)
    const result = await send('Page.captureScreenshot', { format: 'png' })
    await writeFile(output + '/' + name, Buffer.from(result.data, 'base64'))
    report.screenshots.push(name)
  }
  const mouse = (type, x, y, options = {}) => send('Input.dispatchMouseEvent', { type, x, y, ...options })
  async function clickAt(x, y, modifiers = 0) {
    await mouse('mouseMoved', x, y, { modifiers })
    await mouse('mousePressed', x, y, { button: 'left', buttons: 1, clickCount: 1, modifiers })
    await mouse('mouseReleased', x, y, { button: 'left', buttons: 0, clickCount: 1, modifiers })
    await delay(100)
  }
  async function click(selector, modifiers = 0) {
    const rect = await evaluate(`(${rectExpression})(document.querySelector(${JSON.stringify(selector)}))`)
    await clickAt(rect.x + rect.width / 2, rect.y + rect.height / 2, modifiers)
  }
  async function drag(from, to, { button = 'left', buttons = 1, modifiers = 0 } = {}) {
    await mouse('mouseMoved', from.x, from.y, { modifiers })
    await mouse('mousePressed', from.x, from.y, { button, buttons, clickCount: 1, modifiers })
    for (let step = 1; step <= 8; step++) {
      await mouse('mouseMoved', from.x + (to.x - from.x) * step / 8, from.y + (to.y - from.y) * step / 8, { button, buttons, modifiers })
      await delay(25)
    }
    await mouse('mouseReleased', to.x, to.y, { button, buttons: 0, clickCount: 1, modifiers })
    await delay(160)
  }
  async function key(key, code = key, modifiers = 0, text) {
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key, code, modifiers, ...(text ? { text } : {}) })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code, modifiers })
    await delay(80)
  }
  async function focusWithTab(selector) {
    for (let step = 0; step < 45; step++) {
      if (await evaluate(`document.activeElement?.matches(${JSON.stringify(selector)})`)) return step
      await key('Tab', 'Tab')
    }
    throw new Error('Native Tab did not focus: ' + selector)
  }
  async function input(selector, value) {
    await evaluate(`(() => { const element = document.querySelector(${JSON.stringify(selector)}); if (element.isContentEditable) { element.focus(); const range = document.createRange(); range.selectNodeContents(element); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); document.execCommand('insertText', false, ${JSON.stringify(value)}); } else { element.value = ${JSON.stringify(value)}; element.dispatchEvent(new Event('input', { bubbles: true })) } })()`)
  }
  async function createNode(label, x, y) {
    await clickAt(x, y)
    await mouse('mousePressed', x, y, { button: 'left', buttons: 1, clickCount: 2 })
    await mouse('mouseReleased', x, y, { button: 'left', buttons: 0, clickCount: 2 })
    await until(`Boolean(document.querySelector('.create-node-menu'))`)
    const selector = await evaluate(`(() => { const buttons = Array.from(document.querySelectorAll('.create-node-menu button')); const index = buttons.findIndex(button => button.textContent.includes(${JSON.stringify(label)})); return '.create-node-menu button:nth-of-type(' + (index + 1) + ')' })()`)
    await click(selector)
    await until(`Boolean(document.querySelector('.generator-inline-panel'))`)
    const selected = await evaluate(`document.querySelector('.vue-flow__node-generator.selected')?.dataset.id`)
    assert.ok(selected, 'Created generator is selected')
    await click('[aria-label="关闭生成参数"]')
    return selected
  }
  const viewport = () => evaluate(`(() => { const element = document.querySelector('.vue-flow__transformationpane'); const transform = new DOMMatrix(getComputedStyle(element).transform); return { x: transform.e, y: transform.f, zoom: transform.a } })()`)
  const nodeRect = id => evaluate(`(${rectExpression})(document.querySelector('[data-id="${id}"]'))`)
  const selected = () => evaluate(`Array.from(document.querySelectorAll('.vue-flow__node.selected')).map(node => node.dataset.id).sort()`)
  const nodeCount = () => evaluate(`document.querySelectorAll('.vue-flow__node-generator').length`)
  const sameZoom = (before, after, reason) => assert.ok(Math.abs(before.zoom - after.zoom) < .001, reason)
  async function measureSelectionPadding() {
    return evaluate(`(() => {
      const rect = element => { const r = element.getBoundingClientRect(); return { x:r.x, y:r.y, right:r.right, bottom:r.bottom } };
      const nodes = Array.from(document.querySelectorAll('.vue-flow__node.selected')).map(rect);
      const frame = rect(document.querySelector('.vue-flow__nodesselection-rect'));
      const bounds = { x:Math.min(...nodes.map(n => n.x)), y:Math.min(...nodes.map(n => n.y)), right:Math.max(...nodes.map(n => n.right)), bottom:Math.max(...nodes.map(n => n.bottom)) };
      return { frame, bounds, padding:{ left:bounds.x-frame.x, top:bounds.y-frame.y, right:frame.right-bounds.right, bottom:frame.bottom-bounds.bottom } };
    })()`)
  }
  async function assertChromeLayout(width) {
    await send('Emulation.setDeviceMetricsOverride', { width, height: 1050, deviceScaleFactor: 1, mobile: false })
    await delay(200)
    const layout = await evaluate(`(() => {
      const rect = ${rectExpression};
      const toolbar = document.querySelector('.toolbar-actions');
      const nav = document.querySelector('.project-canvas-navigation');
      const actions = Array.from(toolbar.querySelectorAll(':scope > .cosmic-action'));
      const tools = document.querySelector('.canvas-tools');
      const wrap = document.querySelector('.canvas-wrap');
      return { nav:rect(nav), actions:actions.map(rect), chrome:rect(document.querySelector('.chrome-controls')), tools:rect(tools), minimap:rect(document.querySelector('.tactical-minimap')), wrap:rect(wrap), windowWidth:innerWidth, windowHeight:innerHeight, navIsFirst:toolbar.firstElementChild===nav, guideCount:document.querySelectorAll('[aria-label="操作引导"],.onboarding,.onboarding-overlay').length };
    })()`)
    assert.equal(layout.navIsFirst, true, 'Project navigation is the first item inside the right toolbar')
    assert.equal(layout.actions.length, 4, 'Only undo, layout, style and export remain')
    assert.equal(layout.guideCount, 0, 'The operation guide has no mounted DOM')
    const controls = [layout.nav, ...layout.actions]
    const intersects = (a,b) => a.x < b.right && a.right > b.x && a.y < b.bottom && a.bottom > b.y
    for (let i = 0; i < controls.length; i++) {
      for (let j = i + 1; j < controls.length; j++) assert.ok(!intersects(controls[i], controls[j]), 'Right toolbar controls do not overlap')
      assert.ok(!intersects(controls[i], layout.chrome), 'Right toolbar does not cover the persistent panel controls')
      assert.ok(controls[i].x >= 0 && controls[i].right <= width, 'Toolbar control remains inside the viewport')
    }
    assert.ok(layout.nav.y < 60 && layout.nav.x > 110, 'Project navigation lives in the upper-right toolbar')
    assert.ok(layout.actions.at(-1).right <= width - 10, 'Right toolbar stays inside the viewport')
    assert.ok(Math.abs(layout.wrap.bottom - layout.tools.bottom - 16) <= 1, 'Canvas tools stay 16px above the canvas bottom')
    assert.ok(layout.tools.x >= 0 && layout.tools.right <= width, 'Bottom canvas tools stay inside the visible viewport')
    assert.ok(!intersects(layout.tools, layout.minimap), 'Bottom canvas tools do not overlap the minimap')
    report.checks['chrome_' + width] = layout
    await screenshot('00-toolbar-' + width + '.png')
  }

  await send('Page.enable'); await send('Runtime.enable')
  await send('Emulation.setDeviceMetricsOverride', { width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false })
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] })
  // Project CRUD/autosave is allowed only on the explicit QA server. Model/generation writes are blocked.
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__interactionBlockedWrites = [];
    window.__interactionUndoKeys = [];
    window.__interactionClicks = [];
    window.addEventListener('click', event => {
      const target = event.target instanceof Element ? event.target : null;
      window.__interactionClicks.push({ tag: target?.tagName, class: typeof target?.className === 'string' ? target.className : '', node: target?.closest('.vue-flow__node')?.getAttribute('data-id'), meta: event.metaKey, ctrl: event.ctrlKey, shift: event.shiftKey });
      window.__interactionClicks = window.__interactionClicks.slice(-12);
    }, true);
    window.addEventListener('keydown', event => {
      if (event.key.toLowerCase() !== 'z' || !(event.metaKey || event.ctrlKey)) return;
      const trace = { target:event.target?.tagName, className:typeof event.target?.className==='string'?event.target.className:'', url:location.href };
      window.__interactionUndoKeys.push(trace);
      setTimeout(() => { trace.defaultPrevented=event.defaultPrevented; trace.afterUrl=location.href },0);
    }, true);
    const nativeFetch = window.fetch;
    const qaApi = ${JSON.stringify(apiUrl)};
    window.fetch = async function(input, init) {
      const url = String(input?.url || input), method = String(init?.method || input?.method || 'GET').toUpperCase();
      if (url.includes('/api/') && !url.startsWith(qaApi + '/api/')) throw new Error('QA refuses another backend: ' + url);
      if (!['GET', 'HEAD'].includes(method) && url.includes('/api/') && !url.includes('/api/projects')) {
        window.__interactionBlockedWrites.push({ url, method }); throw new Error('QA blocks model/generation submissions');
      }
      if (window.__interactionFixture && method === 'PUT' && url.includes('/api/projects/') && url.endsWith('/canvas')) {
        const body = JSON.parse(init.body); return new Response(JSON.stringify({ project_id: window.__interactionProject, revision: body.expected_revision + 1, state: body.state }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return nativeFetch.call(this, input, init);
    }` })
  await send('Page.navigate', { url: appUrl + '/' })
  await until(`Boolean(document.querySelector('.project-home'))`)
  await click('.projects-title > button')
  await input('.project-dialog input', 'Canvas interaction QA ' + Date.now())
  await click('.project-dialog button[type="submit"]')
  await until(`document.querySelector('.vue-flow__pane') && !document.querySelector('.canvas-restoring')`)
  report.project = await evaluate(`location.pathname.split('/').pop()`)
  const emptyProjectUrl = await evaluate('location.href')
  assert.equal(await evaluate(`document.querySelector('[aria-label="撤回上一步画布操作"]').disabled`), true, 'Fresh empty project has no undo history')
  await key('z', 'KeyZ', 4)
  assert.equal(await evaluate('location.href'), emptyProjectUrl, 'Cmd+Z with no canvas history never navigates away')
  assert.equal(await evaluate('window.__interactionUndoKeys.at(-1)?.defaultPrevented'), true, 'Empty canvas consumes native Cmd+Z to prevent browser undo navigation')
  report.checks.empty_canvas_undo = await evaluate('window.__interactionUndoKeys.at(-1)')
  await assertChromeLayout(1680)
  await send('Emulation.setEmulatedMedia', { features:[{name:'prefers-reduced-motion',value:'no-preference'}] })
  await screenshot('00-toolbar-1680-motion.png')
  await send('Emulation.setEmulatedMedia', { features:[{name:'prefers-reduced-motion',value:'reduce'}] })
  await assertChromeLayout(1280)
  await assertChromeLayout(390)
  await send('Emulation.setDeviceMetricsOverride', { width: 1680, height: 1050, deviceScaleFactor: 1, mobile: false })
  const image = await createNode('图片生成', 410, 230)
  const video = await createNode('视频生成', 900, 230)
  const ids = [image, video].sort()
  assert.equal(await evaluate(`document.querySelector('[aria-label="框选工具"]').getAttribute('aria-pressed')`), 'true')
  const boxes = await Promise.all(ids.map(nodeRect))
  const leftmost = boxes.reduce((left, box) => box.x < left.x ? box : left)
  await drag({ x: leftmost.x - 35, y: leftmost.y - 35 }, { x: leftmost.x + 20, y: leftmost.bottom + 35 })
  assert.deepEqual(await selected(), [], 'A marquee that only overlaps a node must not select it')
  report.checks.full_containment_rejects_edge_overlap = true
  await drag({ x: leftmost.x - 35, y: leftmost.y - 35 }, { x: leftmost.right + 35, y: leftmost.bottom + 35 })
  await until(`document.querySelectorAll('.vue-flow__node.selected').length === 1`)
  const singlePadding = await measureSelectionPadding()
  for (const amount of Object.values(singlePadding.padding)) assert.ok(Math.abs(amount - 12) <= 1.5, 'A single fully-enclosed node also gets a padded selection frame')
  report.checks.full_containment_accepts_one = singlePadding
  await drag({ x: Math.min(...boxes.map(box => box.x)) - 35, y: Math.min(...boxes.map(box => box.y)) - 35 }, { x: Math.max(...boxes.map(box => box.right)) + 35, y: Math.max(...boxes.map(box => box.bottom)) + 35 })
  await until(`document.querySelector('.canvas-selection-count')?.textContent.includes('2')`)
  assert.deepEqual(await selected(), ids, 'Physical marquee selects both generators')
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`), 0)
  const paddingAt100 = await measureSelectionPadding()
  for (const amount of Object.values(paddingAt100.padding)) assert.ok(Math.abs(amount - 12) <= 1.5, 'Selected group has 12px screen padding at 100%')
  await mouse('mouseWheel', 1350, 650, { deltaX: 0, deltaY: 100, modifiers: 2 })
  await delay(180)
  const selectionZoom = await viewport()
  const paddingZoomed = await measureSelectionPadding()
  assert.ok(selectionZoom.zoom < .95, 'Selection remains measurable after zooming out')
  for (const amount of Object.values(paddingZoomed.padding)) assert.ok(Math.abs(amount - 12) <= 1.5, 'Selected group keeps 12px screen padding after zoom')
  report.checks.selection_padding = { paddingAt100, paddingZoomed, zoom:selectionZoom.zoom }
  await screenshot('01-selection-padding-zoomed.png')
  await mouse('mouseWheel', 1350, 650, { deltaX: 0, deltaY: -100, modifiers: 2 })
  await delay(180)
  await screenshot('01-marquee-two-nodes.png')
  const beforeMove = await Promise.all(ids.map(nodeRect))
  const frame = (await measureSelectionPadding()).frame
  // Start inside the 12px outer strip rather than a node: the enlarged frame must remain draggable.
  await drag({ x: frame.x + 5, y: frame.y + 45 }, { x: frame.x + 105, y: frame.y + 105 })
  const afterMove = await Promise.all(ids.map(nodeRect))
  const deltas = afterMove.map((box, index) => ({ x: box.x - beforeMove[index].x, y: box.y - beforeMove[index].y }))
  assert.ok(deltas[0].x > 60 && deltas[0].y > 30, 'Group moves under a native mouse drag')
  assert.ok(Math.abs(deltas[0].x - deltas[1].x) < 1 && Math.abs(deltas[0].y - deltas[1].y) < 1, 'Both nodes move by the same displacement')
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`), 0, 'Dragging does not open a composer')
  report.checks.marquee_and_group_move = { ids, deltas }
  assert.equal(await evaluate(`document.activeElement?.matches('.vue-flow__nodesselection-rect')`), true, 'Native selection frame owns keyboard focus after marquee and group drag')
  await key('z', 'KeyZ', 4)
  const frameUndo = await Promise.all(ids.map(nodeRect))
  frameUndo.forEach((box,index) => { assert.ok(Math.abs(box.x-beforeMove[index].x)<1 && Math.abs(box.y-beforeMove[index].y)<1, 'Cmd+Z from selection-frame focus restores the group position') })
  assert.equal(await evaluate('window.__interactionUndoKeys.at(-1)?.defaultPrevented'), true)
  report.checks.selection_frame_undo = await evaluate('window.__interactionUndoKeys.at(-1)')
  await drag({ x:Math.min(...beforeMove.map(box=>box.x))-35,y:Math.min(...beforeMove.map(box=>box.y))-35 },{ x:Math.max(...beforeMove.map(box=>box.right))+35,y:Math.max(...beforeMove.map(box=>box.bottom))+35 })
  await until(`document.querySelectorAll('.vue-flow__node.selected').length === 2 && Boolean(document.querySelector('.vue-flow__nodesselection-rect'))`)
  const nextFrame = (await measureSelectionPadding()).frame
  await drag({ x:nextFrame.x+5, y:nextFrame.y+45 },{ x:nextFrame.x+105,y:nextFrame.y+105 })
  const tabSteps = await focusWithTab('.vue-flow__node')
  await key('z', 'KeyZ', 4)
  const nodeUndo = await Promise.all(ids.map(nodeRect))
  nodeUndo.forEach((box,index) => { assert.ok(Math.abs(box.x-beforeMove[index].x)<1 && Math.abs(box.y-beforeMove[index].y)<1, 'Cmd+Z from node-wrapper focus restores the group position') })
  assert.equal(await evaluate('window.__interactionUndoKeys.at(-1)?.defaultPrevented'), true)
  assert.equal(await evaluate('location.href'),emptyProjectUrl)
  report.checks.node_focused_undo = { tabSteps,...await evaluate('window.__interactionUndoKeys.at(-1)') }
  // Native Tab can leave a single wrapper focused; explicitly reselect the full group before batch delete.
  await drag({ x:Math.min(...beforeMove.map(box=>box.x))-35,y:Math.min(...beforeMove.map(box=>box.y))-35 },{ x:Math.max(...beforeMove.map(box=>box.right))+35,y:Math.max(...beforeMove.map(box=>box.bottom))+35 })
  assert.deepEqual(await selected(),ids)
  await key('Delete', 'Delete')
  await until(`document.querySelectorAll('.vue-flow__node-generator').length === 0`)
  await key('z', 'KeyZ', 4)
  await until(`document.querySelectorAll('.vue-flow__node-generator').length === 2`)
  assert.equal(await nodeCount(), 2, 'One Cmd+Z restores the whole deleted group')
  report.checks.batch_delete_one_undo = true
  await click(`[data-id="${image}"] .generator-card`)
  await until(`Boolean(document.querySelector('.inline-prompt'))`)
  await input('.inline-prompt', 'Edit-safeABC')
  await click('.inline-prompt')
  await key('Backspace', 'Backspace')
  await key('Delete', 'Delete')
  await key('z', 'KeyZ', 4)
  await key('h', 'KeyH', 0, 'h')
  assert.equal(await nodeCount(), 2, 'Editor keys cannot delete canvas nodes or undo their creation')
  assert.equal(await evaluate(`document.querySelector('[aria-label="框选工具"]').getAttribute('aria-pressed')`), 'true', 'Typing H does not switch tools')
  const beforeEditorWheel = await viewport()
  const prompt = await evaluate(`(${rectExpression})(document.querySelector('.inline-prompt'))`)
  await mouse('mouseWheel', prompt.x + 50, prompt.y + 30, { deltaX: 0, deltaY: 80 })
  await delay(140)
  assert.deepEqual(await viewport(), beforeEditorWheel, 'Scrolling the editor does not pan the canvas')
  await click('[aria-label="关闭生成参数"]')
  // The editor must yield keyboard ownership after a real pointer down on the node surface.
  await click(`[data-id="${image}"] .generator-card`)
  await click('.inline-prompt')
  await send('Input.insertText', { text:' native editor QA' })
  await click(`[data-id="${image}"] .generator-card`)
  assert.equal(await evaluate(`document.activeElement?.matches('input,textarea,[contenteditable="true"]')`),false,'Clicking a node surface releases stale text-editor focus')
  await key('Delete','Delete')
  await until(`document.querySelectorAll('.vue-flow__node-generator').length === 1`)
  await key('z','KeyZ',4)
  await until(`document.querySelectorAll('.vue-flow__node-generator').length === 2`)
  report.checks.editor_to_canvas_delete_undo=true
  await clickAt(1350, 650)
  const plainBefore = await viewport()
  await mouse('mouseWheel', 1350, 650, { deltaX: 35, deltaY: 70 })
  await delay(180)
  const plainAfter = await viewport()
  report.checks.wheel = { plainBefore, plainAfter, diagnostic: await evaluate(`(() => { const setup = document.querySelector('.app-shell').__vueParentComponent.setupState; const target = document.elementFromPoint(1350,650); return { target: target?.className?.baseVal || target?.className, marqueeSelecting: setup.marqueeSelecting, nodeDragInProgress: setup.nodeDragInProgress, restoring: setup.restoringCanvas, createMenu: Boolean(setup.createMenu), style: setup.styleStudioOpen, eventAgent: setup.eventAgentOpen, activeElement: document.activeElement?.tagName, viewport: setup.viewport } })()`) }
  await screenshot('02-wheel-diagnostic.png')
  sameZoom(plainBefore, plainAfter, 'Plain wheel preserves zoom')
  assert.ok(Math.abs(plainAfter.x - plainBefore.x) > 10 && Math.abs(plainAfter.y - plainBefore.y) > 10, 'Plain wheel pans both axes')
  await mouse('mouseWheel', 1350, 650, { deltaX: 0, deltaY: -100, modifiers: 2 })
  await delay(180)
  const controlAfter = await viewport()
  assert.ok(controlAfter.zoom > plainAfter.zoom, 'Ctrl+wheel zooms the canvas')
  await mouse('mouseWheel', 1350, 650, { deltaX: 0, deltaY: 45, modifiers: 4 })
  await delay(180)
  const metaAfter = await viewport()
  assert.ok(metaAfter.zoom < controlAfter.zoom, 'Cmd+wheel zooms the canvas')
  report.checks.wheel = { plainBefore, plainAfter, controlAfter, metaAfter }
  await click('[aria-label="显示全部节点"]')
  await delay(200)
  await clickAt(1350,650)
  await key('h', 'KeyH')
  await until(`document.querySelector('[aria-label="抓手工具"]').getAttribute('aria-pressed') === 'true'`)
  const handBefore = await viewport()
  const handNode = await nodeRect(image)
  const localTransforms = () => evaluate(`Array.from(document.querySelectorAll('.vue-flow__node-generator')).map(node => ({ id: node.dataset.id, transform: node.style.transform })).sort((a,b) => a.id.localeCompare(b.id))`)
  const handTransforms = await localTransforms()
  await drag({ x: handNode.x + handNode.width / 2, y: handNode.y + 45 }, { x: handNode.x + handNode.width / 2 + 60, y: handNode.y + 85 })
  const handAfter = await viewport()
  sameZoom(handBefore, handAfter, 'Hand drag preserves zoom')
  assert.ok(Math.abs(handAfter.x - handBefore.x) > 30, 'H enables left-button panning')
  assert.deepEqual(await localTransforms(), handTransforms, 'Hand dragging across a node does not move that node')
  await key('v', 'KeyV')
  const spaceBefore = await viewport()
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, modifiers: 0 })
  await until(`document.querySelector('.canvas-wrap').classList.contains('canvas-tool-hand')`)
  await drag({ x: 1200, y: 650 }, { x: 1240, y: 680 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, modifiers: 0 })
  await until(`document.querySelector('.canvas-wrap').classList.contains('canvas-tool-select')`)
  const spaceAfter = await viewport()
  assert.ok(Math.abs(spaceAfter.x - spaceBefore.x) > 20, 'Space enables temporary panning')
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, modifiers: 0 })
  await until(`document.querySelector('.canvas-wrap').classList.contains('canvas-tool-hand')`)
  await evaluate(`window.dispatchEvent(new Event('blur'))`)
  await until(`document.querySelector('.canvas-wrap').classList.contains('canvas-tool-select')`)
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ' ', code: 'Space', windowsVirtualKeyCode: 32, modifiers: 0 })
  const middleBefore = await viewport()
  await drag({ x: 1200, y: 650 }, { x: 1240, y: 680 }, { button: 'middle', buttons: 4 })
  const middleAfter = await viewport()
  assert.ok(Math.abs(middleAfter.x - middleBefore.x) > 20, 'Middle mouse pans')
  report.checks.hand_space_middle = { handBefore, handAfter, spaceBefore, spaceAfter, middleBefore, middleAfter, nodeTransformsPreserved: true, spaceReleaseRestoresSelect: true, blurRestoresSelect: true }
  const mini = await evaluate(`(${rectExpression})(document.querySelector('.tactical-minimap > svg'))`)
  const mapBefore = await viewport()
  await drag({ x: mini.x + 90, y: mini.y + 45 }, { x: mini.x + 120, y: mini.y + 65 })
  const mapAfter = await viewport()
  sameZoom(mapBefore, mapAfter, 'Minimap dragging preserves zoom')
  report.checks.minimap = await evaluate(`({ header: Boolean(document.querySelector('.tactical-head')), corners: document.querySelectorAll('.hud-corner').length, legendRows: document.querySelectorAll('.tactical-foot').length, rectangles: Array.from(document.querySelectorAll('[data-minimap-kind="image-generation"] rect,[data-minimap-kind="video-generation"] rect')).map(rect => ({ width: Number(rect.getAttribute('width')), height: Number(rect.getAttribute('height')), fill: rect.getAttribute('fill') })) })`)
  assert.equal(report.checks.minimap.header, false); assert.equal(report.checks.minimap.corners, 0); assert.equal(report.checks.minimap.legendRows, 1)
  assert.equal(report.checks.minimap.rectangles.length, 2)
  await screenshot('02-minimap-and-tools.png')

  // Saved observations provide scene/shot nodes without a cloud call. Autosaves of this fixture stay in-browser.
  const fixture = await evaluate(`(async () => {
    window.__interactionFixture = true; window.__interactionProject = ${JSON.stringify(report.project)};
    const { sequenceBranchTask } = await import('/tests/fixtures/sequenceBranches.ts');
    const setup = document.querySelector('.app-shell').__vueParentComponent.setupState;
    const task = sequenceBranchTask('interaction-scene', 2);
    task.jobId = null; task.generationSequences = []; task.narrativeEvents = []; task.position = { x: 40, y: 90 };
    for (const shot of task.shots) { const image = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#3a6070"/></svg>'); shot.thumb = image; shot.frames.forEach(frame => frame.url = image) }
    setup.generators = []; setup.generatorConnections = [];
    setup.store.tasks = []; setup.store.restoreTask(task); setup.buildGraph();
    return { root: task.key, shots: task.shots.map(shot => shot.id) };
  })()`)
  await click('[aria-label="显示全部节点"]')
  await until(`Boolean(document.querySelector('.vue-flow__node-scene .scene-deck-main'))`)
  await delay(180)
  assert.equal(await evaluate(`document.querySelector('.scene-deck-main').getAttribute('aria-expanded')`), 'false')
  await click('.scene-deck-main', 4)
  assert.equal(await evaluate(`document.querySelector('.scene-deck-main').getAttribute('aria-expanded')`), 'false', 'Cmd+click only selects the scene')
  await click('.scene-deck-main', 8)
  assert.equal(await evaluate(`document.querySelector('.scene-deck-main').getAttribute('aria-expanded')`), 'false', 'Shift+click only adjusts selection')
  await click('.scene-deck-main')
  report.checks.scene_diagnostic = await evaluate(`(() => { const card = document.querySelector('.scene-deck-main'); const r = card.getBoundingClientRect(); const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2); const setup = document.querySelector('.app-shell').__vueParentComponent.setupState; return { rect: (${rectExpression})(card), hit: hit?.className, expanded: card.getAttribute('aria-expanded'), clicks: window.__interactionClicks, flags: { draggedClickId: setup.draggedClickId, marqueeSelecting: setup.marqueeSelecting, nodeDragInProgress: setup.nodeDragInProgress, class: document.querySelector('.canvas-wrap').className }, sceneUi: setup.sceneUi } })()`)
  await screenshot('03-scene-diagnostic.png')
  await until(`document.querySelector('.scene-deck-main').getAttribute('aria-expanded') === 'true'`)
  await click('[aria-label="显示全部节点"]')
  await delay(180)
  const shotId = fixture.shots[0]
  await click(`[data-id="${shotId}"] .shot-node`)
  assert.equal(await evaluate(`document.querySelectorAll('[data-canvas-details]').length`),0,'Selecting a source shot does not automatically open details')
  const shotBeforeDrag=await nodeRect(shotId)
  await drag({x:shotBeforeDrag.x+shotBeforeDrag.width/2,y:shotBeforeDrag.y+35},{x:shotBeforeDrag.x+shotBeforeDrag.width/2+45,y:shotBeforeDrag.y+65})
  const shotAfterDrag=await nodeRect(shotId)
  assert.ok(Math.abs(shotAfterDrag.x-shotBeforeDrag.x)>20,'A source-node drag creates a fixture-local undo entry')
  const canvasBeforeDetails=await evaluate(`(${rectExpression})(document.querySelector('.canvas-wrap'))`)
  const viewportBeforeDetails=await viewport()
  await key('i','KeyI',0,'i')
  await until(`Boolean(document.querySelector('[data-canvas-details][data-detail-kind="shot"]'))`)
  assert.equal(await evaluate(`document.querySelector('[data-canvas-details]').getAttribute('aria-modal')`),'false','Details are non-modal')
  assert.deepEqual(await evaluate(`(${rectExpression})(document.querySelector('.canvas-wrap'))`),canvasBeforeDetails,'Details do not change canvas dimensions')
  assert.deepEqual(await viewport(),viewportBeforeDetails,'Details do not pan or zoom the canvas')
  await click('.node-details-prompt-links button')
  await until(`Boolean(document.querySelector('[data-canvas-details][data-detail-kind="prompt"] textarea[readonly]'))`)
  await click('[data-canvas-details] textarea[readonly]')
  await screenshot('04-readonly-prompt-details.png')
  await key('z','KeyZ',4)
  const restoredSource=await nodeRect(shotId)
  assert.ok(Math.abs(restoredSource.x-shotBeforeDrag.x)<1 && Math.abs(restoredSource.y-shotBeforeDrag.y)<1,'Cmd+Z from readonly Prompt details restores the last canvas move')
  assert.equal(await evaluate('window.__interactionUndoKeys.at(-1)?.defaultPrevented'),true)
  assert.equal(await evaluate('location.href'),emptyProjectUrl)
  report.checks.explicit_details_and_readonly_undo={canvasUnchanged:true,viewportUnchanged:true,undo:await evaluate('window.__interactionUndoKeys.at(-1)')}
  // Creation of a generator closes the non-modal detail host and opens only its own composer.
  await createNode('图片生成',1400,780)
  assert.equal(await evaluate(`document.querySelectorAll('[data-canvas-details]').length`),0,'A generator composer and source details are mutually exclusive')
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`),0,'createNode helper explicitly closes its one composer')
  report.checks.generator_detail_exclusion=true
  await click(`[data-id="${shotId}"] .shot-node`)
  await key('i','KeyI',0,'i')
  await until(`Boolean(document.querySelector('[data-canvas-details]'))`)
  await click('[aria-label="关闭详情"]')
  assert.equal(await evaluate(`document.querySelectorAll('[data-canvas-details]').length`),0)
  await click(`[data-id="${shotId}"] .shot-node`)
  await key('Delete', 'Delete')
  await until(`!document.querySelector('[data-id="${shotId}"]') || document.querySelector('[data-id="${shotId}"]').getBoundingClientRect().width === 0`)
  assert.equal(await evaluate(`document.querySelector('.app-shell').__vueParentComponent.setupState.store.tasks.find(task => task.key === ${JSON.stringify(fixture.root)}).shots.length`), 2, 'Hiding a derived node preserves analysis data')
  await click('.canvas-restore-hidden')
  await until(`document.querySelector('[data-id="${shotId}"]')?.getBoundingClientRect().width > 0`)
  report.checks.modifier_scene_and_derived_restore = true
  await screenshot('03-scene-and-derived-restore.png')
  if(await evaluate(`Boolean(document.querySelector('.movie-breadcrumb'))`))await click('.movie-breadcrumb')
  const detailOwners=await evaluate(`(async()=>{
    const{sequenceBranchTask}=await import('/tests/fixtures/sequenceBranches.ts');const setup=document.querySelector('.app-shell').__vueParentComponent.setupState;
    setup.generators=[];setup.generatorConnections=[];setup.store.tasks=[];
    const tasks=[sequenceBranchTask('detail-owner-a',2),sequenceBranchTask('detail-owner-b',2)];
    tasks.forEach((task,index)=>{task.jobId=null;task.position={x:40,y:80+index*620};for(const shot of task.shots){const image='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#3a6070"/></svg>');shot.thumb=image;shot.frames.forEach(frame=>frame.url=image)}setup.store.restoreTask(task)});
    setup.buildGraph();return tasks.map(task=>({root:task.key,source:task.shots[0].id,sequence:task.generationSequences[0].id}));
  })()`)
  await click('[aria-label="显示全部节点"]');await delay(180)
  await click(`[data-id="${detailOwners[0].sequence}"] .sequence-kicker`)
  await key('i','KeyI',0,'i')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[0].sequence)}`)
  await click('#node-details-tab-evidence')
  await click('[data-testid="sequence-source-members"] .sequence-member')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[0].source)}`)
  await click('.node-details-return')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[0].sequence)}`)
  // The second task's root remains outside the centered detail host and is a native pointer target.
  const otherRoot=await evaluate(`(()=>{const element=document.querySelector('[data-id="${detailOwners[1].root}"] .project-node h3');const r=element.getBoundingClientRect();const x=r.x+8,y=r.y+r.height/2;const hit=document.elementFromPoint(x,y);return{x,y,rect:(${rectExpression})(element),hit:hit?.className,node:hit?.closest('.vue-flow__node')?.getAttribute('data-id')}})()`)
  assert.equal(otherRoot.node,detailOwners[1].root,'Cross-task native click targets the visible root surface outside the floating host')
  await clickAt(otherRoot.x,otherRoot.y)
  report.checks.cross_task_selection_diagnostic={otherRoot,state:await evaluate(`(()=>{const setup=document.querySelector('.app-shell').__vueParentComponent.setupState;return{selected:setup.store.selectedId,detailId:document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id'),clicks:window.__interactionClicks.slice(-3)}})()`)}
  assert.equal(await evaluate(`document.querySelectorAll('[data-canvas-details]').length`),0,'Switching tasks closes the previous task detail owner')
  await key('i','KeyI',0,'i')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[1].root)}`)
  await click('#node-details-tab-evidence')
  await click('.node-details-source-list > button')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[1].source)}`)
  await click('.node-details-return')
  await until(`document.querySelector('[data-canvas-details]')?.getAttribute('data-detail-id')===${JSON.stringify(detailOwners[1].sequence)}`)
  report.checks.multitask_detail_returns=detailOwners
  await screenshot('05-multitask-sequence-details.png')
  assert.deepEqual(await evaluate('window.__interactionBlockedWrites'), [], 'No model or generation writes were attempted')
  assert.deepEqual(report.errors, [])
  report.passed = true
  await writeFile(output + '/report.json', JSON.stringify(report, null, 2))
  console.log(JSON.stringify({ passed: true, project: report.project, checks: Object.keys(report.checks), output }))
} catch (error) {
  report.failure = String(error)
  await writeFile(output + '/report.json', JSON.stringify(report, null, 2))
  throw error
} finally { socket?.close(); chrome.kill('SIGTERM') }
