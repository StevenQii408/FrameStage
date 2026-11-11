/** Real Safari / W3C WebDriver. Uses isolated automation windows and an explicit Mock backend. */
import { spawn } from 'node:child_process'
import { mkdir, writeFile } from 'node:fs/promises'
import { temporaryDirectory } from './browser-environment.mjs'
import assert from 'node:assert/strict'

const args=Object.fromEntries(process.argv.slice(2).flatMap((value,index,all)=>value.startsWith('--')?[[value.slice(2),all[index+1]]]:[]))
assert.equal(args['qa-only'],'yes','Pass --qa-only yes and explicit isolated --url/--api addresses')
assert.ok(args.url&&args.api,'Isolated frontend and backend URLs are required')
const appUrl=args.url.replace(/\/$/,''),apiUrl=args.api.replace(/\/$/,'')
assert.equal(new URL(apiUrl).hostname,'127.0.0.1');assert.notEqual(new URL(apiUrl).port,'8000')
assert.equal(new URL(appUrl).hostname,'127.0.0.1');assert.notEqual(new URL(appUrl).port,'5174')
const output=args.output||await temporaryDirectory('frameforge-safari-undo-')
await mkdir(output,{recursive:true})
const port=Number(args['driver-port']||48319)
const driverUrl='http://127.0.0.1:'+port
const driver=spawn('/usr/bin/safaridriver',['--port',String(port)],{stdio:'ignore'})
const report={browser:'Safari (actual safaridriver automation window; not WKWebView)',checks:{},errors:[]}
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms))
let session
async function request(path,method='GET',body){
 const response=await fetch(driverUrl+path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(20000)})
 const result=await response.json();if(result.value?.error){const error=new Error(result.value.message);error.webDriverError=result.value.error;throw error}return result.value
}
const command=(path,method='GET',body)=>request('/session/'+session+path,method,body)
async function evaluate(expression){const result=await command('/execute/async','POST',{script:'const done=arguments[arguments.length-1];Promise.resolve().then(()=>('+expression+')).then(value=>done({value}),error=>done({error:String(error)}));',args:[]});if(result.error)throw Error(result.error);return result.value}
async function until(expression){for(let n=0;n<160;n++){if(await evaluate(expression))return;await pause(100)}throw Error('Safari condition not met: '+expression)}
async function element(selector){const value=await command('/element','POST',{using:'css selector',value:selector});return value['element-6066-11e4-a52e-4f735466cecf']}
async function click(selector){await command('/element/'+await element(selector)+'/click','POST',{});await pause(100)}
async function text(selector,value){await command('/element/'+await element(selector)+'/value','POST',{text:value})}
async function key(value,meta=false){await command('/actions','POST',{actions:[{type:'key',id:'keyboard',actions:[...(meta?[{type:'keyDown',value:'\uE03D'}]:[]),{type:'keyDown',value},{type:'keyUp',value},...(meta?[{type:'keyUp',value:'\uE03D'}]:[])]}]});await pause(100)}
async function clickAt(x,y,count=1){const actions=[];for(let n=0;n<count;n++)actions.push({type:'pointerMove',duration:0,origin:'viewport',x:Math.round(x),y:Math.round(y)},{type:'pointerDown',button:0},{type:'pointerUp',button:0});await command('/actions','POST',{actions:[{type:'pointer',id:'mouse',parameters:{pointerType:'mouse'},actions}]});await pause(120)}
async function drag(from,to){await command('/actions','POST',{actions:[{type:'pointer',id:'mouse',parameters:{pointerType:'mouse'},actions:[{type:'pointerMove',duration:0,origin:'viewport',x:Math.round(from.x),y:Math.round(from.y)},{type:'pointerDown',button:0},...Array.from({length:8},(_,i)=>({type:'pointerMove',duration:30,origin:'viewport',x:Math.round(from.x+(to.x-from.x)*(i+1)/8),y:Math.round(from.y+(to.y-from.y)*(i+1)/8)})),{type:'pointerUp',button:0}]}]});await pause(180)}
const rect=selector=>evaluate(`(()=>{const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();return{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}})()`)
async function screenshot(name){await writeFile(output+'/'+name,Buffer.from(await command('/screenshot'),'base64'))}
async function metadata(){return evaluate(`(()=>{const el=document.activeElement;const setup=document.querySelector('.app-shell')?.__vueParentComponent?.setupState;return{focus:{tag:el?.tagName,readOnly:el?.readOnly===true,editable:el?.isContentEditable===true,details:Boolean(el?.closest('[data-canvas-details]'))},canvasHistory:setup?.history?.length,browserHistory:window.history.length,url:location.href,generators:document.querySelectorAll('.vue-flow__node-generator').length,keys:window.__safariProbe.keys.slice(-3),inputs:window.__safariProbe.inputs.slice(-3)}})()`)}
async function undo(label,verify){const before=await metadata();await key('z',true);await pause(150);const after=await metadata();assert.equal(after.url,before.url,label+' keeps the project URL');assert.equal(after.browserHistory,before.browserHistory,label+' keeps browser history');assert.equal(after.keys.at(-1)?.trusted,true,label+' receives a trusted Safari event');assert.equal(after.keys.at(-1)?.prevented,true,label+' prevents native fallback');if(verify)await verify(before,after);report.checks[label]={before,after}}
async function createImage(){await clickAt(410,230);await clickAt(410,230,2);await until(`Boolean(document.querySelector('.create-node-menu'))`);const selector=await evaluate(`(()=>{const buttons=[...document.querySelectorAll('.create-node-menu button')];return '.create-node-menu button:nth-of-type('+(buttons.findIndex(item=>item.textContent.includes('图片生成'))+1)+')'})()`);await click(selector);await until(`Boolean(document.querySelector('.inline-prompt'))`);return evaluate(`document.querySelector('.vue-flow__node-generator.selected').dataset.id`)}
try{
 let reachable=false;for(let n=0;n<70;n++){try{await request('/status');reachable=true;break}catch{await pause(100)}}assert.ok(reachable,'Local Safari WebDriver starts')
 try{const opened=await request('/session','POST',{capabilities:{alwaysMatch:{browserName:'safari','safari:automaticInspection':false,'safari:automaticProfiling':false}}});session=opened.sessionId;report.capabilities=opened.capabilities}catch(error){report.blocked=error.webDriverError==='session not created';throw error}
 await command('/timeouts','POST',{script:20000,pageLoad:30000,implicit:0})
 await command('/window/rect','POST',{width:1680,height:1100})
 // Install the capture probe on the project home before Workbench mounts its capture listener.
 // This keeps Safari events observable without altering the Undo implementation.
 await command('/url','POST',{url:appUrl+'/projects'})
 await until(`Boolean(document.querySelector('.project-home'))`)
 await evaluate(`(()=>{window.__safariProbe={keys:[],inputs:[],errors:[],blocked:[]};window.addEventListener('keydown',event=>{if(!(event.metaKey||event.ctrlKey)||event.key.toLowerCase()!=='z')return;const row={key:event.key,code:event.code,keyCode:event.keyCode,meta:event.metaKey,ctrl:event.ctrlKey,trusted:event.isTrusted,composing:event.isComposing,target:event.target?.tagName,readOnly:event.target?.readOnly===true,details:Boolean(event.target?.closest('[data-canvas-details]'))};window.__safariProbe.keys.push(row);setTimeout(()=>{row.prevented=event.defaultPrevented},0)},true);window.addEventListener('beforeinput',event=>{if(!['historyUndo','historyRedo'].includes(event.inputType))return;const row={inputType:event.inputType,trusted:event.isTrusted,target:event.target?.tagName,readOnly:event.target?.readOnly===true};window.__safariProbe.inputs.push(row);setTimeout(()=>{row.prevented=event.defaultPrevented},0)},true);window.addEventListener('error',event=>window.__safariProbe.errors.push(String(event.message)));const nativeFetch=window.fetch;window.fetch=async function(input,init){const url=String(input?.url||input),method=String(init?.method||input?.method||'GET').toUpperCase();if(url.includes('/api/')&&!url.startsWith(${JSON.stringify(apiUrl+'/api/')}))throw Error('QA refuses another backend');if(!['GET','HEAD'].includes(method)&&url.includes('/api/')&&!url.includes('/api/projects')){window.__safariProbe.blocked.push({url,method});throw Error('QA blocks paid submissions')}return nativeFetch.call(this,input,init)};return true})()`)
 await click('.projects-title > button');await text('.project-dialog input','Safari native undo QA '+Date.now());await click('.project-dialog button[type="submit"]')
 await until(`document.querySelector('.canvas-wrap')&&!document.querySelector('.canvas-restoring')`)
 report.project=await evaluate(`location.pathname.split('/').pop()`)
 await clickAt(1350,700);await undo('empty_history',(before,after)=>assert.equal(before.canvasHistory,after.canvasHistory))
 const image=await createImage()
 await click('.inline-prompt');await text('.inline-prompt','Safari text QA')
 const editableBefore=await metadata();const textBefore=await evaluate(`document.querySelector('.inline-prompt').textContent`);await key('z',true);const editableAfter=await metadata();assert.equal(editableAfter.generators,editableBefore.generators);assert.equal(editableAfter.canvasHistory,editableBefore.canvasHistory);assert.equal(editableAfter.keys.at(-1)?.prevented,true);assert.notEqual(await evaluate(`document.querySelector('.inline-prompt').textContent`),textBefore);report.checks.prompt_text_undo={before:editableBefore,after:editableAfter}
 await text('.inline-prompt',' after text edit');await click(`[data-id="${image}"] .generator-card`)
 assert.notEqual((await metadata()).focus.tag,'TEXTAREA','Safari node click releases stale editor focus')
 await key('\uE003');await until(`document.querySelectorAll('.vue-flow__node-generator').length===0`)
 await undo('editor_to_canvas_delete',(before,after)=>assert.equal(after.generators,1))
 await click(`[data-id="${image}"] .generator-card`);await click('.inline-prompt');await text('.inline-prompt',' before move')
 const generatorBefore=await rect(`[data-id="${image}"]`)
 await drag({x:generatorBefore.x+generatorBefore.width/2,y:generatorBefore.y+35},{x:generatorBefore.x+generatorBefore.width/2+70,y:generatorBefore.y+75})
 await undo('editor_to_canvas_move',async()=>{const box=await rect(`[data-id="${image}"]`);assert.ok(Math.abs(box.x-generatorBefore.x)<1&&Math.abs(box.y-generatorBefore.y)<1)})
 const fixture=await evaluate(`(async()=>{window.__safariFixture=true;const{sequenceBranchTask}=await import('/tests/fixtures/sequenceBranches.ts');const setup=document.querySelector('.app-shell').__vueParentComponent.setupState;const task=sequenceBranchTask('safari-real',2);task.jobId=null;task.generationSequences=[];task.narrativeEvents=[];task.position={x:40,y:520};for(const shot of task.shots){const image='data:image/svg+xml,'+encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="#3a6070"/></svg>');shot.thumb=image;shot.frames.forEach(frame=>frame.url=image)}setup.store.restoreTask(task);setup.sceneUi[task.key+':scene:scene-shared-name']={open:true,page:1};setup.buildGraph();return{source:task.shots[0].id}})()`)
 await click('[aria-label="显示全部节点"]');await pause(400)
 const sourceBefore=await rect(`[data-id="${fixture.source}"]`)
 await drag({x:sourceBefore.x+sourceBefore.width/2,y:sourceBefore.y+35},{x:sourceBefore.x+sourceBefore.width/2+45,y:sourceBefore.y+65})
 await key('i');await until(`Boolean(document.querySelector('[data-canvas-details][data-detail-kind="shot"]'))`)
 await undo('non_modal_host_focus',async()=>{const box=await rect(`[data-id="${fixture.source}"]`);assert.ok(Math.abs(box.x-sourceBefore.x)<1&&Math.abs(box.y-sourceBefore.y)<1)})
 if(await evaluate(`Boolean(document.querySelector('[aria-label="关闭详情"]'))`))await click('[aria-label="关闭详情"]')
 const nextSource=await rect(`[data-id="${fixture.source}"]`)
 await drag({x:nextSource.x+nextSource.width/2,y:nextSource.y+35},{x:nextSource.x+nextSource.width/2+45,y:nextSource.y+65})
 await key('i');await until(`Boolean(document.querySelector('[data-canvas-details][data-detail-kind="shot"]'))`)
 await click('.node-details-prompt-links button');await until(`Boolean(document.querySelector('[data-canvas-details] textarea[readonly]'))`);await click('[data-canvas-details] textarea[readonly]')
 await screenshot('safari-readonly-prompt.png')
 await undo('readonly_prompt_focus',async before=>{assert.equal(before.focus.readOnly,true);const box=await rect(`[data-id="${fixture.source}"]`);assert.ok(Math.abs(box.x-nextSource.x)<1&&Math.abs(box.y-nextSource.y)<1)})
 assert.deepEqual(await evaluate('window.__safariProbe.blocked'),[])
 report.errors=await evaluate('window.__safariProbe.errors');assert.deepEqual(report.errors,[])
 report.passed=true
}catch(error){report.failure=String(error);if(session){try{report.lastMetadata=await metadata();await screenshot('safari-failure.png')}catch{}}throw error}
finally{await writeFile(output+'/report.json',JSON.stringify(report,null,2));if(session){try{await request('/session/'+session,'DELETE')}catch{}}driver.kill('SIGTERM')}
console.log(JSON.stringify({passed:report.passed,project:report.project,checks:Object.keys(report.checks),output}))
