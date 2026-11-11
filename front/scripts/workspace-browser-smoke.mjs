/** Isolated Chrome integration test. Creates QA projects; never invokes a paid API. */
import { spawn } from 'node:child_process'
import { readFile, writeFile, mkdtemp, mkdir } from 'node:fs/promises'
import { chromeExecutable, temporaryDirectory } from './browser-environment.mjs'
import assert from 'node:assert/strict'
import { verifyMockBranches } from './sequence-browser-fixtures.mjs'
const args=Object.fromEntries(process.argv.slice(2).flatMap((value,index,all)=>value.startsWith('--')?[[value.slice(2),all[index+1]]]:[]))
const root=args.output||await temporaryDirectory('frameforge-workspace-browser-')
await mkdir(root,{recursive:true})
const appUrl=args.url||'http://127.0.0.1:5174'
const apiBase=args.api||'http://127.0.0.1:8000'
const progressTest=args.progress==='yes'
const nameA = 'QA A '+Date.now(), nameB='QA B '+Date.now()
const initialCapabilities=await(await fetch(apiBase+'/api/generations/capabilities')).json()
assert.equal(initialCapabilities.configured,false,'This test requires a disabled generation Provider; never run against a paid generation service')
const profile = await mkdtemp(root + '/chrome-')
const chrome = spawn(await chromeExecutable(args.chrome), ['--headless=new','--disable-gpu','--no-first-run','--disable-background-networking','--disable-extensions','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'], { stdio:'ignore' })
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))
let socket, setFixtureMode, fixtureEnabled=false, captureFailure=async()=>({})
try {
  let port
  for (let n=0;n<100;n++) { try { port=(await readFile(profile+'/DevToolsActivePort','utf8')).split('\n')[0];break } catch { await delay(100) } }
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
  socket = new WebSocket(targets.find(item=>item.type==='page').webSocketDebuggerUrl)
  await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true})})
  let id=0;const pending=new Map(), errors=[]
  socket.addEventListener('message',event=>{const message=JSON.parse(event.data);if(message.id){const call=pending.get(message.id);pending.delete(message.id);message.error?call?.reject(new Error(JSON.stringify(message.error))):call?.resolve(message.result)}else if(message.method==='Runtime.exceptionThrown')errors.push(message.params.exceptionDetails.exception?.description||message.params.exceptionDetails.text);else if(message.method==='Page.javascriptDialogOpening')void send('Page.handleJavaScriptDialog',{accept:true}).catch(()=>undefined)})
  const send=(method,params={})=>new Promise((resolve,reject)=>{const number=++id;const timer=setTimeout(()=>reject(new Error('CDP timeout '+method)),15000);pending.set(number,{resolve:value=>{clearTimeout(timer);resolve(value)},reject:error=>{clearTimeout(timer);reject(error)}});socket.send(JSON.stringify({id:number,method,params}))})
  async function evaluate(expression){let result;try{result=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true})}catch(error){throw new Error(`${error.message}: ${expression.slice(0,220)}`)}if(result.exceptionDetails)throw new Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);return result.result.value}
  // Run fixture changes from the same trusted workbench origin as production writes.
  // Keep the bootstrap token inside the isolated browser, including during cleanup.
  setFixtureMode = mode => evaluate(`(async () => {
    const base = ${JSON.stringify(apiBase)}
    const session = await fetch(base + '/api/session', {cache: 'no-store'})
    if (!session.ok) throw new Error('QA session bootstrap failed: ' + session.status)
    const {token} = await session.json()
    const response = await fetch(base + '/__qa/provider/' + ${JSON.stringify(mode)}, {
      method: 'POST', redirect: 'error', headers: {'X-FrameForge-Session': token}
    })
    if (!response.ok) throw new Error('QA fixture rejected mode: ' + response.status)
  })()`)
  async function until(expression){for(let n=0;n<160;n++){if(await evaluate(expression))return;await delay(100)}throw new Error('Condition not met '+expression)}
  const setup=`document.querySelector('.app-shell').__vueParentComponent.setupState`
  async function click(selector){await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`)}
  async function button(text){await evaluate(`Array.from(document.querySelectorAll('button')).find(item=>item.textContent.trim()===${JSON.stringify(text)}||item.getAttribute('aria-label')===${JSON.stringify(text)}).click()`)}
  async function input(selector,value){await evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});if(input.isContentEditable){input.focus();const range=document.createRange();range.selectNodeContents(input);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);document.execCommand('insertText',false,${JSON.stringify(value)});}else{input.value=${JSON.stringify(value)};input.dispatchEvent(new Event('input',{bubbles:true}));}})()`)}
  async function shot(name){const result=await send('Page.captureScreenshot',{format:'png'});await writeFile(root+'/'+name,Buffer.from(result.data,'base64'))}
  captureFailure=async()=>{await shot('failure.png');return evaluate(`({url:location.href,text:document.body.innerText.slice(0,4000),errors:${JSON.stringify(errors)}})`)}
  async function createProject(name){await button('新建项目');await input('.project-dialog input',name);await evaluate(`document.querySelector('.project-dialog').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))`);await until(`Boolean(document.querySelector('.vue-flow__pane')) && !document.querySelector('.canvas-restoring')`);return evaluate(`location.pathname.split('/').pop()`)}
  async function createNode(name,x,y){await evaluate(`document.querySelector('.vue-flow__pane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true,clientX:${x},clientY:${y}}))`);await until(`Boolean(document.querySelector('.create-node-menu'))`);await evaluate(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);await evaluate(`Array.from(document.querySelectorAll('.create-node-menu button')).find(item=>item.textContent.includes(${JSON.stringify(name)})).click()`);await until(`document.querySelectorAll('.generator-inline-panel').length===1`);return evaluate(`${setup}.store.selectedId`)}
  await send('Page.enable');await send('Runtime.enable');await send('DOM.enable')
  await send('Page.addScriptToEvaluateOnNewDocument',{source:`window.__qaPromptText=(selector)=>{const element=document.querySelector(selector);if(!element)return null;if(!element.isContentEditable)return element.value;const read=node=>node.nodeType===3?node.textContent:node.nodeType!==1?'':node.hasAttribute('data-reference-token')?node.getAttribute('data-reference-token'):node.tagName==='BR'?'\\n':Array.from(node.childNodes).map(read).join('');return Array.from(element.childNodes).map(read).join('');}`})
  await send('Emulation.setDeviceMetricsOverride',{width:1600,height:1050,deviceScaleFactor:1,mobile:false})
  await send('Page.navigate',{url:appUrl+'/'})
  await until(`Boolean(document.querySelector('.project-home')) && !document.querySelector('.projects-empty')?.textContent.includes('正在读取')`)
  const projectA=await createProject(nameA)
  assert.equal(await evaluate(`document.querySelector('.inspector')===null`),true)
  const image=await createNode('图片生成',440,210)
  // Match the real video reference-image constraints, including the minimum 240 px side.
  const referenceImage = await evaluate(`(() => {
    const canvas = document.createElement('canvas')
    canvas.width = 320
    canvas.height = 480
    const ctx = canvas.getContext('2d')
    ctx.fillStyle = '#567d86'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    return canvas.toDataURL('image/png').split(',')[1]
  })()`)
  await writeFile(root+'/reference.png', Buffer.from(referenceImage, 'base64'))
  const documentNode=await send('DOM.getDocument')
  const fileInput=await send('DOM.querySelector',{nodeId:documentNode.root.nodeId,selector:'.generator-inline-panel input[type=file]'})
  await send('DOM.setFileInputFiles',{nodeId:fileInput.nodeId,files:[root+'/reference.png']})
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(image)})?.nodeMode==='reference' && document.querySelectorAll('.generator-inline-panel').length===0 && document.querySelector('[data-id="${image}"] .generator-card img')`)
  await evaluate(`${setup}.select(${JSON.stringify(image)})`)
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`),0,'a manual reference asset must not open generation controls')
  assert.equal(await evaluate(`document.querySelector('[data-id="${image}"] .generator-handle-input')===null`),true,'reference assets expose an output only')
  assert.equal(await evaluate(`Boolean(document.querySelector('[data-id="${image}"] .generator-handle-output'))`),true)
  const referenceRatio=await evaluate(`(()=>{const card=document.querySelector('[data-id="${image}"] .generator-card');return parseFloat(card.style.width)/parseFloat(card.style.height)})()`)
  assert.ok(Math.abs(referenceRatio-320/480)<0.001,'imported reference must keep its original portrait ratio')
  await shot('reference-asset.png')
  const video=await createNode('视频生成',900,210)
  await evaluate(`${setup}.onConnect({source:${JSON.stringify(image)},sourceHandle:'output',target:${JSON.stringify(video)},targetHandle:'input'})`)
  await evaluate(`${setup}.select(${JSON.stringify(video)})`)
  await until(`document.querySelectorAll('.inline-reference img').length===1`)
  assert.equal(await evaluate(`document.querySelectorAll('.generator-card img').length`),1,'connected reference must not become downstream output')
  await input('.inline-prompt','以@')
  await until(`document.querySelectorAll('[role=listbox] [role=option]').length===1`)
  await click('[role=listbox] [role=option]')
  await until(`window.__qaPromptText('.inline-prompt')?.includes('@图片1')`)
  await until(`Boolean(document.querySelector('.inline-prompt [data-reference-token="@图片1"] img'))`)
  const chipChecks=[]
  const thumb=await evaluate(`(()=>{const image=document.querySelector('.inline-prompt [data-reference-token] img');const r=image.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,width:r.width,height:r.height,fit:getComputedStyle(image).objectFit}})()`)
  assert.ok(Math.abs(thumb.width-thumb.height)<.1,'inline reference thumbnails are square')
  assert.equal(thumb.fit,'cover')
  await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:thumb.x,y:thumb.y})
  await until(`(()=>{const image=document.querySelector('.reference-thumbnail-preview img');return image?.complete&&image.naturalWidth>0})()`)
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.reference-thumbnail-preview img')).objectFit`),'contain','hover preview shows the full original image')
  await shot('reference-chip-hover.png')
  await send('Input.dispatchMouseEvent',{type:'mouseMoved',x:10,y:10})
  await until(`!document.querySelector('.reference-thumbnail-preview')`)
  const originalPrompt=await evaluate(`window.__qaPromptText('.inline-prompt')`)
  await evaluate(`(()=>{const editor=document.querySelector('.inline-prompt');editor.focus();const range=document.createRange();range.selectNodeContents(editor);range.collapse(false);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);})()`)
  await send('Input.insertText',{text:'保持自然光'})
  await until(`window.__qaPromptText('.inline-prompt')?.endsWith('保持自然光')`)
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await until(`window.__qaPromptText('.inline-prompt')===${JSON.stringify(originalPrompt)}`)
  assert.equal(await evaluate(`${setup}.generators.length`),2,'text Undo must not undo canvas node creation')
  await evaluate(`(()=>{const editor=document.querySelector('.inline-prompt');editor.focus();const range=document.createRange();range.setStartAfter(editor.querySelector('[data-reference-token]'));range.collapse(true);const selection=window.getSelection();selection.removeAllRanges();selection.addRange(range);})()`)
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Backspace',code:'Backspace',windowsVirtualKeyCode:8})
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Backspace',code:'Backspace',windowsVirtualKeyCode:8})
  await until(`!document.querySelector('.inline-prompt [data-reference-token]')`)
  assert.equal(await evaluate(`${setup}.generators.length`),2,'deleting a chip must not delete a node')
  assert.equal(await evaluate(`${setup}.generatorConnections.length`),1,'deleting a chip must not disconnect the reference asset')
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await until(`window.__qaPromptText('.inline-prompt')===${JSON.stringify(originalPrompt)}&&Boolean(document.querySelector('.inline-prompt [data-reference-token]'))`)
  chipChecks.push('square-inline-thumbnail','full-hover-preview','text-undo-isolated','atomic-chip-delete','chip-undo-retains-reference')
  await click('[aria-label="展开完整 Prompt"]')
  await input('[aria-label="完整 Prompt"]','以@图片1 为首帧，微风吹动树叶，镜头缓慢向前。')
  await button('完成编辑')
  await until(`window.__qaPromptText('.inline-prompt')?.includes('微风')`)
  await button('校验并保存任务')
  await until(`document.querySelector('.generator-inline-panel')?.textContent.includes('等待接入生成 API')`)
  const generation=await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(video)}).generationTask.jobId`)
  const blocked=await(await fetch(apiBase+'/api/generations/'+generation+'?project_id='+projectA)).json()
  assert.equal(blocked.status,'blocked');assert.equal(blocked.progress,null);assert.equal(blocked.assets.length,0)
  assert.equal(blocked.request.reference_images[0].mention,'图片1')
  await shot('references-inline.png')
  // A selected edge can be deleted and restored without deleting either node.
  const wire=await evaluate(`${setup}.generatorConnections[0].id`)
  await evaluate(`${setup}.handleEdgeClick({edge:{id:${JSON.stringify(wire)}}})`)
  await until(`document.querySelector('.energy-edge-selected')`)
  await evaluate(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true,cancelable:true}))`)
  await until(`${setup}.generatorConnections.length===0`)
  assert.equal(await evaluate(`${setup}.generators.length`),2)
  await evaluate(`window.dispatchEvent(new KeyboardEvent('keydown',{key:'z',metaKey:true,bubbles:true,cancelable:true}))`)
  await until(`${setup}.generatorConnections.length===1`)
  await click('[aria-label="返回项目列表"]');await until(`Boolean(document.querySelector('.project-home'))`)
  const projectB=await createProject(nameB)
  assert.equal(await evaluate(`${setup}.generators.length`),0)
  await click('[aria-label="返回项目列表"]');await until(`Boolean(document.querySelector('.project-home'))`)
  await click('[aria-label="进入项目 '+nameA+'"]')
  await until(`document.querySelectorAll('.generator-card img').length===1`)
  await evaluate(`${setup}.select(${JSON.stringify(video)})`)
  await until(`window.__qaPromptText('.inline-prompt')?.includes('@图片1')`)
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`),1)
  await shot('project-restored.png')
  const progress=[];let progressNode=null,historyTarget=null,historyVersions=[]
  if(progressTest){
  // Only the explicitly started local fixture can enable a fake.
  await setFixtureMode('test')
  fixtureEnabled=true
  const fixtureCapabilities=await(await fetch(apiBase+'/api/generations/capabilities')).json()
  assert.equal(fixtureCapabilities.provider,'local-qa-only','Never exercise generation against a paid service')
  await until(`JSON.parse(localStorage.getItem('frameforge-project:'+${JSON.stringify(projectA)}+':canvas-v1')||'null')?.dirty===false`)
  await evaluate(`window.__qaReloadMarker=true`)
  await send('Page.reload');await until(`!window.__qaReloadMarker && document.querySelector('.vue-flow__pane') && !document.querySelector('.canvas-restoring') && document.querySelectorAll('.generator-card img').length===1`)
  progressNode=await createNode('图片生成',500,650)
  await input('.inline-prompt','仅用于本地 SSE 验收的蓝色测试图片。')
  if(!await evaluate(`Boolean(document.querySelector('[aria-label="生成分辨率"]'))`)) await button('生成规格')
  await evaluate(`(()=>{const select=document.querySelector('[aria-label="生成分辨率"]');select.value='720p';select.dispatchEvent(new Event('change',{bubbles:true}))})()`)
  if(await evaluate(`Boolean(document.querySelector('[aria-label="生成规格"][aria-expanded="true"]'))`)) await button('生成规格')
  await button('生成')
  await until(`document.querySelector('[data-id="${progressNode}"] [role=progressbar]')?.getAttribute('aria-valuenow')==='20'`)
  const tideLevels=[Number(await evaluate(`document.querySelector('[data-id="${progressNode}"] [data-tide-level]')?.getAttribute('data-tide-level')`))]
  assert.equal(tideLevels[0],20,'star-sea fill must track the displayed provider percentage')
  await shot('sse-running.png')
  for(let n=0;n<14;n++){const value=await evaluate(`document.querySelector('[data-id="${progressNode}"] .generator-external-label')?.textContent`);progress.push(value);if(value?.includes('80%')&&tideLevels.length===1){tideLevels.push(Number(await evaluate(`document.querySelector('[data-id="${progressNode}"] [data-tide-level]')?.getAttribute('data-tide-level')`)));await shot('sse-tide-risen.png')}if(value?.includes('生成完成'))break;await delay(500)}
  await until(`document.querySelector('[data-id="${progressNode}"] .generator-external-label')?.textContent.includes('生成完成')`)
  assert.ok(progress.some(value=>value?.includes('%')))
  assert.deepEqual(tideLevels,[20,80],'star-sea fill must visibly advance as SSE progress increases')
  await shot('sse-completed.png')
  assert.equal(await evaluate(`document.querySelectorAll('[data-id="${progressNode}"] .vue-flow__handle').length`),2)
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)})?.versions?.length===1`)
  const firstVersion=await evaluate(`JSON.parse(JSON.stringify(${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).versions[0]))`)
  assert.equal(firstVersion.prompt,'仅用于本地 SSE 验收的蓝色测试图片。')
  assert.equal(firstVersion.asset.mime_type,'image/png')
  // The fixture may emit the same image bytes twice: version/job identities must still be distinct.
  historyTarget=await createNode('视频生成',900,650)
  await evaluate(`${setup}.onConnect({source:${JSON.stringify(progressNode)},sourceHandle:'output',target:${JSON.stringify(historyTarget)},targetHandle:'input'})`)
  await until(`${setup}.resolvedGenerator(${JSON.stringify(historyTarget)}).references?.some(item=>item.sourceNodeId===${JSON.stringify(progressNode)})`)
  assert.equal(await evaluate(`document.querySelector('[data-id="${historyTarget}"] .generator-card img')===null`),true,'a generated reference must not become a downstream output')
  await evaluate(`${setup}.select(${JSON.stringify(progressNode)})`)
  await until(`window.__qaPromptText('.inline-prompt')===${JSON.stringify(firstVersion.prompt)}`)
  const secondPrompt='第二版：仅用于本机历史版本验收，保留自然光与蓝绿色调。'
  await input('.inline-prompt',secondPrompt)
  await button('生成')
  await until(`document.querySelector('[data-id="${progressNode}"] [role=progressbar]')?.getAttribute('aria-valuenow')==='20'`)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).versions.length`),1,'starting a new attempt must retain the delivered version')
  assert.equal(await evaluate(`document.querySelector('[data-id="${progressNode}"] .generator-card img')?.getAttribute('src')`),firstVersion.asset.url)
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)})?.versions?.length===2`)
  const secondVersion=await evaluate(`JSON.parse(JSON.stringify(${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).versions[0]))`)
  assert.notEqual(firstVersion.id,secondVersion.id)
  assert.notEqual(firstVersion.jobId,secondVersion.jobId)
  assert.equal(secondVersion.prompt,secondPrompt)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).activeVersionId`),secondVersion.id,'new delivered output is the default version')
  await click('[data-id="'+progressNode+'"] [aria-label="查看生成历史"]')
  await until(`document.querySelectorAll('[data-generator-history] li').length===2`)
  assert.equal(await evaluate(`document.querySelectorAll('.generator-inline-panel').length`),0,'history and the generation composer are mutually exclusive')
  assert.equal(await evaluate(`document.querySelector('[data-generator-history]').getAttribute('aria-modal')`),'false')
  assert.equal(await evaluate(`document.querySelector('[data-generator-history] [data-version-id="${firstVersion.id}"] .history-prompt').textContent`),firstVersion.prompt)
  await click('[data-generator-history] [data-version-id="'+firstVersion.id+'"] button')
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).activeVersionId===${JSON.stringify(firstVersion.id)}`)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).manualPrompt`),secondPrompt,'pinning a result must not replace the editable Prompt')
  assert.equal(await evaluate(`document.querySelector('[data-id="${progressNode}"] .generator-card img')?.getAttribute('src')`),firstVersion.asset.url)
  const downstream=await evaluate(`JSON.parse(JSON.stringify(${setup}.resolvedGenerator(${JSON.stringify(historyTarget)}).references.find(item=>item.sourceNodeId===${JSON.stringify(progressNode)})))`)
  assert.equal(downstream.url,firstVersion.asset.url)
  assert.equal(downstream.identity,'generated:'+firstVersion.asset.id)
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4})
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).activeVersionId===${JSON.stringify(secondVersion.id)}`)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).manualPrompt`),secondPrompt,'Undoing a pin keeps the editable draft')
  await click('[data-id="'+progressNode+'"] [aria-label="查看生成历史"]')
  await until(`document.querySelectorAll('[data-generator-history] li').length===2`)
  await click('[data-generator-history] [data-version-id="'+firstVersion.id+'"] button')
  await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).activeVersionId===${JSON.stringify(firstVersion.id)}`)
  await shot('history-pinned.png')
  // Wait for server persistence, not an arbitrary delay, before testing refresh recovery.
  await until(`(async()=>{const response=await fetch(${JSON.stringify(apiBase+'/api/projects/'+projectA+'/canvas')});const canvas=await response.json();const item=canvas.state?.generators?.find(node=>node.id===${JSON.stringify(progressNode)});return item?.versions?.length===2&&item.activeVersionId===${JSON.stringify(firstVersion.id)}})()`)
  await evaluate(`window.__qaHistoryReloadMarker=true`)
  await send('Page.reload')
  await until(`!window.__qaHistoryReloadMarker && document.querySelector('.vue-flow__pane') && !document.querySelector('.canvas-restoring') && ${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)})?.versions?.length===2`)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).activeVersionId`),firstVersion.id)
  assert.equal(await evaluate(`${setup}.generators.find(item=>item.id===${JSON.stringify(progressNode)}).manualPrompt`),secondPrompt)
  const restoredReference=await evaluate(`JSON.parse(JSON.stringify(${setup}.resolvedGenerator(${JSON.stringify(historyTarget)}).references.find(item=>item.sourceNodeId===${JSON.stringify(progressNode)})))`)
  assert.equal(restoredReference.url,firstVersion.asset.url)
  assert.equal(restoredReference.identity,'generated:'+firstVersion.asset.id)
  await evaluate(`${setup}.select(${JSON.stringify(progressNode)})`)
  await until(`window.__qaPromptText('.inline-prompt')===${JSON.stringify(secondPrompt)}`)
  assert.match(await evaluate(`document.querySelector('[aria-label="生成规格"]').textContent.trim()`),/16:9.*(?:720[pP]|1280\s*×\s*720)/)
  await shot('history-restored.png')
  historyVersions=[firstVersion.id,secondVersion.id]
  }
  const mediaChecks=[]
  if(args.media==='yes'){
    assert.equal(fixtureEnabled,true,'Media checks require the explicit non-paid QA provider')
    await evaluate(`${setup}.select(${JSON.stringify(video)})`)
    await until(`Boolean(document.querySelector('.inline-prompt'))`)
    await input('.inline-prompt','固定镜头的本地测试图案视频，不调用任何真实生成服务。')
    if(!await evaluate(`Boolean(document.querySelector('[aria-label="生成分辨率"]'))`)) await button('生成规格')
    await evaluate(`(()=>{const input=document.querySelector('[aria-label="生成分辨率"]');input.value='720p';input.dispatchEvent(new Event('change',{bubbles:true}))})()`)
    const modelLabel=await evaluate(`document.querySelector('[aria-label="生成模型"]')?.selectedOptions[0]?.textContent`)
    assert.ok(modelLabel&&!modelLabel.includes('默认'),'a concrete model name must be displayed')
    if(await evaluate(`Boolean(document.querySelector('[aria-label="生成规格"][aria-expanded="true"]'))`)) await button('生成规格')
    await button('生成')
    await until(`Boolean(document.querySelector('[data-id="${video}"] .generation-wave-progress'))`)
    await shot('video-wave-progress.png')
    await until(`(()=>{const poster=document.querySelector('[data-id="${video}"] .generator-video-poster');return poster?.complete&&poster.naturalWidth>0})()`)
    assert.equal(await evaluate(`document.querySelectorAll('[data-id="${video}"] .generator-card video').length`),0,'collapsed video uses a static poster instead of decoding all videos')
    assert.equal(await evaluate(`getComputedStyle(document.querySelector('[data-id="${video}"] .generator-video-poster')).objectFit`),'contain')
    await shot('video-poster.png')
    await click('[data-id="'+video+'"] [aria-label="播放生成视频"]')
    await until(`(()=>{const media=document.querySelector('[data-id="${video}"] .canvas-video-player video');return media?.readyState>=2&&media.videoWidth===1280&&media.videoHeight===720})()`)
    await shot('video-inline-playback.png')
    await click('[data-id="'+video+'"] [aria-label="收起节点预览"]')
    mediaChecks.push('concrete-model-label','wave-progress','video-poster-visible','collapsed-no-video-decoding','inline-playback')
  }
  const failureChecks=[]
  if(args.failures==='yes'){
    assert.equal(fixtureEnabled,true,'Failure checks require the explicit non-paid QA provider')
    assert.ok(progressNode&&args.media==='yes','Failure cases exercise both delivered image and video histories')
    const cases=[
      ['capacity','provider_capacity_exhausted',true,progressNode],
      ['rate','rate_limited',true,video],
      ['balance','provider_balance_insufficient',false,progressNode],
      ['content','content_rejected',false,video],
      ['auth','provider_authentication_failed',false,progressNode],
      ['timeout','provider_timeout',true,video],
      ['unknown','provider_failed',false,progressNode],
    ]
    for(const [mode,code,retryable,nodeId] of cases){
      await setFixtureMode('test-fail-'+mode)
      const snapshot=await evaluate(`(()=>{const item=${setup}.generators.find(item=>item.id===${JSON.stringify(nodeId)});return{jobId:item.generationTask?.jobId,versions:item.versions.length,asset:${setup}.generationTasks.outputFor(item.id)?.url}})()`)
      await evaluate(`${setup}.select(${JSON.stringify(nodeId)})`)
      await until(`Boolean(document.querySelector('.inline-prompt'))`)
      await input('.inline-prompt','本机失败反馈验收：'+mode+'。')
      await until(`!document.querySelector('[data-id="${nodeId}"] [data-generation-feedback]')`)
      await button('生成')
      await until(`${setup}.generators.find(item=>item.id===${JSON.stringify(nodeId)})?.generationTask?.jobId!==${JSON.stringify(snapshot.jobId)}`)
      await until(`Boolean(document.querySelector('[data-id="${nodeId}"] [data-generation-feedback][role="alert"]'))`)
      const failure=await evaluate(`(()=>{const item=${setup}.generators.find(item=>item.id===${JSON.stringify(nodeId)});return{jobId:item.generationTask.jobId,versions:item.versions.length,asset:${setup}.generationTasks.outputFor(item.id)?.url,text:document.querySelector('[data-id="${nodeId}"] [data-generation-feedback]').textContent}})()`)
      assert.match(failure.text,/生成失败/)
      if(mode==='capacity')assert.match(failure.text,/上游算力不足/)
      assert.ok(!failure.text.includes('QA_UPSTREAM'),'raw upstream diagnostics must not appear on the card')
      assert.equal(failure.versions,snapshot.versions,'failed regenerations must not add a delivered version')
      assert.equal(failure.asset,snapshot.asset,'failed regenerations retain the pinned successful output')
      assert.equal(await evaluate(`document.querySelector('[data-id="${nodeId}"] .generation-starfield-progress')===null`),true,'failure stops the tide effect immediately')
      const job=await(await fetch(apiBase+'/api/generations/'+failure.jobId+'?project_id='+projectA)).json()
      assert.equal(job.status,'failed');assert.equal(job.error.code,code);assert.equal(job.error.retryable,retryable)
      assert.equal(job.assets.length,0);assert.notEqual(job.progress,100)
      if(mode==='capacity')await shot('failure-capacity-retains-image.png')
      if(mode==='rate')await shot('failure-rate-retains-video.png')
      failureChecks.push({mode,code,retryable,retained:true})
    }
    await setFixtureMode('test')
  }
  let mockCases=[]
  if(args.mock==='yes'){
    const nodeInfo=`Array.from(document.querySelectorAll('.vue-flow__node')).map(node=>({id:node.dataset.id,transform:node.style.transform,type:node.className,rect:((r)=>({x:r.x,y:r.y,width:r.width,height:r.height}))(node.getBoundingClientRect())})).filter(node=>node.rect.width>0&&node.rect.height>0)`
    const assertNoOverlap=nodes=>{for(let a=0;a<nodes.length;a++)for(let b=a+1;b<nodes.length;b++){const l=nodes[a].rect,r=nodes[b].rect;assert.ok(!(l.x<r.x+r.width-2&&l.x+l.width-2>r.x&&l.y<r.y+r.height-2&&l.y+l.height-2>r.y),nodes[a].id+' overlaps '+nodes[b].id)}}
    await evaluate(`${setup}.generators=[];${setup}.generatorConnections=[];void 0`)
    mockCases=await verifyMockBranches({evaluate,waitFor:until,nodeInfo,assertNoOverlap,screenshot:shot,assert})
  }
  assert.deepEqual(errors,[])
  await writeFile(root+'/report.json',JSON.stringify({projectA,projectB,image,video,progressNode,historyTarget,historyVersions,generation,progress,chipChecks,mediaChecks,failureChecks,mockCases,errors,passed:true},null,2))
  console.log(JSON.stringify({passed:true,projectA,projectB,progress,historyVersions,chipChecks,mediaChecks,failureChecks,mockCases,errors,output:root}))
} catch(error) {const diagnostics=await captureFailure().catch(()=>({}));await writeFile(root+'/failure.json',JSON.stringify({message:error.message,...diagnostics},null,2));throw error}
finally {if(fixtureEnabled)await setFixtureMode('disabled').catch(()=>undefined);socket?.close();chrome.kill()}
