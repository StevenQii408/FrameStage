/**
 * Normal Safari + Cocoa menu regression, not WebDriver or WKWebView.
 * Requires the user's existing Safari Apple Events JavaScript and osascript AX permission.
 * Business setup is an in-memory QA fixture; Cmd+Z is sent through System Events.
 * A native HID click first gives our own page WebKit keyboard focus.
 */
import {execFile} from 'node:child_process'
import {promisify} from 'node:util'
import {mkdir,writeFile} from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { resolve, sep } from 'node:path'
import { temporaryDirectory } from './browser-environment.mjs'
import assert from 'node:assert/strict'
const args=Object.fromEntries(process.argv.slice(2).flatMap((value,index,all)=>value.startsWith('--')?[[value.slice(2),all[index+1]]]:[]))
assert.equal(args['qa-only'],'yes','Pass --qa-only yes and explicit isolated --url/--api addresses')
assert.equal(args.url,'http://127.0.0.1:5280','Native Safari QA must use the isolated 5280 frontend')
assert.equal(args.api,'http://127.0.0.1:8267','Native Safari QA must use the isolated 8267 Mock backend')
const run=promisify(execFile),app=args.url,api=args.api,output=resolve(args.output||await temporaryDirectory('frameforge-safari-native-'))
assert.ok([resolve(tmpdir()), '/tmp', '/private/tmp'].some(root => output.startsWith(root + sep)),'QA artifacts must stay in a temporary directory')
await mkdir(output,{recursive:true})
const report={browser:'Normal Safari window + System Events native keys (not WebDriver)',checks:{}}
const wait=ms=>new Promise(r=>setTimeout(r,ms));let windowId,serial=0,expectedProject,contentOrigin,previousFrontPid
async function jxa(source){const{stdout}=await run('osascript',['-l','JavaScript','-e',source],{timeout:10000,maxBuffer:300000});return stdout.trim()}
async function apple(source){const{stdout}=await run('osascript',['-e',source],{timeout:10000,maxBuffer:300000});return stdout.trim()}
async function js(expression){const body=`(()=>{try{return JSON.stringify({ok:true,value:(0,eval)(${JSON.stringify(expression)})})}catch(error){return JSON.stringify({ok:false,error:String(error)})}})()`;const text=await jxa(`const s=Application('Safari');const w=s.windows.byId(${windowId});const t=${expectedProject?`w.tabs().find(t=>t.url()===${JSON.stringify(expectedProject)})`:'w.currentTab()'};s.doJavaScript(${JSON.stringify(body)},{in:t});`);if(!text)throw Error('Safari JavaScript returned no probe response');const result=JSON.parse(text);if(!result.ok)throw Error(result.error);return result.value}
async function asyncJs(expression){const id='call'+(++serial);await js(`(()=>{window.__nativeQaReplies||={};Promise.resolve(${expression}).then(value=>window.__nativeQaReplies[${JSON.stringify(id)}]={value},error=>window.__nativeQaReplies[${JSON.stringify(id)}]={error:String(error)});return true})()`);for(let n=0;n<100;n++){const value=await js(`window.__nativeQaReplies[${JSON.stringify(id)}]||null`);if(value){if(value.error)throw Error(value.error);return value.value}await wait(100)}throw Error('Async Safari evaluate timeout')}
async function until(expression){for(let n=0;n<150;n++){if(await js(expression))return;await wait(100)}throw Error('Condition not met: '+expression)}
async function activate(){await apple(`tell application "Safari"\nset index of window id ${windowId} to 1\nactivate\nend tell`)}
async function click(selector){await activate();for(let attempt=0;attempt<2;attempt++){const r=await js(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});e.scrollIntoView({block:'nearest',inline:'nearest'});const r=e.getBoundingClientRect();return{clientX:r.x+r.width/2,clientY:r.y+r.height/2,originX:screenX+(outerWidth-innerWidth)/2,originY:screenY+(outerHeight-innerHeight),before:window.__nativeMenuProbe.pointers.length}})()`);const x=Math.round((contentOrigin?.x??r.originX)+r.clientX),y=Math.round((contentOrigin?.y??r.originY)+r.clientY);await jxa(`ObjC.import('ApplicationServices');if(!$.AXIsProcessTrusted())throw Error('Native pointer requires existing AX permission');const point={x:${x},y:${y}};const down=$.CGEventCreateMouseEvent(null,$.kCGEventLeftMouseDown,point,$.kCGMouseButtonLeft);const up=$.CGEventCreateMouseEvent(null,$.kCGEventLeftMouseUp,point,$.kCGMouseButtonLeft);$.CGEventPost($.kCGHIDEventTap,down);$.CGEventPost($.kCGHIDEventTap,up);true;`);await wait(180);const actual=await js(`(()=>{const probe=window.__nativeMenuProbe;const p=probe.pointers.at(-1);return{pointer:p,count:probe.pointers.length,hitExpected:Boolean(probe.lastPointerTarget&&document.querySelector(${JSON.stringify(selector)})?.contains(probe.lastPointerTarget))}})()`);assert.ok(actual.count>r.before,'A native click reaches the owned Safari document');contentOrigin={x:x-actual.pointer.x,y:y-actual.pointer.y};report.pointerOrigin=contentOrigin;if(actual.hitExpected)return;}throw Error('Native pointer did not reach '+selector)}
async function nativeKey(kind){const current=JSON.parse(await jxa(`const s=Application('Safari');JSON.stringify(s.windows.byId(${windowId}).currentTab.url());`));assert.equal(current,expectedProject,'Native key remains inside our own active QA project tab');await activate();await apple(`tell application "Safari"\nif id of front window is not ${windowId} then error "Native QA window is not frontmost"\nif URL of current tab of front window is not ${JSON.stringify(expectedProject)} then error "Native QA tab changed"\nend tell\ntell application "System Events"\nif frontmost of process "Safari" is false then error "Safari is not the active application"\ntell process "Safari" to ${kind}\nend tell`);await wait(220)}
async function info(){return js(`(()=>{const s=document.querySelector('.app-shell')?.__vueParentComponent.setupState;const e=document.activeElement;return{url:location.href,timeOrigin:performance.timeOrigin,documentFocused:document.hasFocus(),focus:{tag:e?.tagName,readOnly:e?.readOnly===true,editable:e?.isContentEditable===true},canvasHistory:s?.history?.length,historyKinds:s?.history?.slice(-4).map(x=>x.kind),browserHistory:history.length,generators:document.querySelectorAll('.vue-flow__node-generator').length,modal:Boolean(document.querySelector('[aria-modal="true"]')),keys:window.__nativeMenuProbe?.keys,inputs:window.__nativeMenuProbe?.inputs,pointers:window.__nativeMenuProbe?.pointers.slice(-4),appUid:document.querySelector('.app-shell')?.__vueParentComponent.uid}})()`)}
async function sentinel(){await jxa(`const s=Application('Safari'),w=s.windows.byId(${windowId});const before=w.tabs().length;w.tabs.push(s.Tab({url:${JSON.stringify(app+'/?qa-native-undo-sentinel=1')}}));if(w.tabs().length!==before+1)throw Error('Sentinel tab count mismatch');w.tabs[before].close();true;`)}
async function createImage(){await js(`document.querySelector('.vue-flow__pane').dispatchEvent(new MouseEvent('dblclick',{bubbles:true,clientX:410,clientY:230}));true`);await until(`Boolean(document.querySelector('.create-node-menu'))`);await js(`Array.from(document.querySelectorAll('.create-node-menu button')).find(b=>b.textContent.includes('图片生成')).click();true`);await until(`Boolean(document.querySelector('.inline-prompt'))`);return js(`document.querySelector('.vue-flow__node-generator.selected').dataset.id`)}
async function afterKey(label,before){const after=await info();const tabs=JSON.parse(await jxa(`const s=Application('Safari'),w=s.windows.byId(${windowId});JSON.stringify(w.tabs().map(t=>t.url()).filter(url=>url.startsWith(${JSON.stringify(app)})));`));report.checks[label]={before,after,tabs};return{before,after,tabs}}
try{
 assert.equal(JSON.parse(await jxa(`ObjC.import('ApplicationServices');JSON.stringify(!!$.AXIsProcessTrusted());`)),true,'Existing osascript Accessibility permission is required; the test never changes it')
 const capabilities=await(await fetch(api+'/api/generations/capabilities')).json();assert.equal(capabilities.configured,false,'The isolated generation Provider must be disabled')
 previousFrontPid=JSON.parse(await jxa(`const p=Application('System Events').applicationProcesses.whose({frontmost:true})()[0];JSON.stringify(p.unixId());`))
 windowId=JSON.parse(await jxa(`const s=Application('Safari');const before=s.windows.id();s.Document({url:${JSON.stringify(app+'/projects')}}).make();JSON.stringify(s.windows.id().find(id=>!before.includes(id)));`))
 await until(`Boolean(document.querySelector('.project-home'))`)
 await js(`(()=>{window.__nativeMenuProbe={keys:[],inputs:[],pointers:[]};window.addEventListener('pointerdown',event=>{const p=window.__nativeMenuProbe;p.lastPointerTarget=event.target;p.pointers.push({x:event.clientX,y:event.clientY,trusted:event.isTrusted,target:event.target?.tagName,node:event.target?.closest('.vue-flow__node')?.getAttribute('data-id')})},true);window.addEventListener('keydown',event=>{if(!(event.metaKey||event.ctrlKey)||event.key.toLowerCase()!=='z')return;const row={key:event.key,code:event.code,meta:event.metaKey,trusted:event.isTrusted,cancelable:event.cancelable,composing:event.isComposing,target:event.target?.tagName,readOnly:event.target?.readOnly===true};window.__nativeMenuProbe.keys.push(row);setTimeout(()=>row.prevented=event.defaultPrevented,0)},true);window.addEventListener('beforeinput',event=>{if(event.inputType!=='historyUndo')return;const row={type:event.inputType,target:event.target?.tagName,trusted:event.isTrusted};window.__nativeMenuProbe.inputs.push(row);setTimeout(()=>row.prevented=event.defaultPrevented,0)},true);return true})()`)
 await js(`(()=>{window.__nativeMenuProbe.blocked=[];const fetchNative=window.fetch;window.fetch=function(input,init){const url=String(input?.url||input),method=String(init?.method||input?.method||'GET').toUpperCase();if(url.includes('/api/')&&!url.startsWith(${JSON.stringify(api+'/api/')}))throw Error('Native QA refuses another backend');if(url.includes('/api/')&&!['GET','HEAD'].includes(method)&&!url.includes('/api/projects')){window.__nativeMenuProbe.blocked.push({url,method});throw Error('Native QA blocks model/generation submissions')}return fetchNative.call(this,input,init)};return true})()`)
 await js(`document.querySelector('.projects-title>button').click();true`)
 await js(`(()=>{const e=document.querySelector('.project-dialog input');e.value='Safari physical menu QA '+Date.now();e.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.project-dialog').requestSubmit();return true})()`)
 await until(`Boolean(document.querySelector('.canvas-wrap'))&&!document.querySelector('.canvas-restoring')`)
 expectedProject=await js('location.href');report.project=expectedProject
 await click('body')
 const image=await createImage();report.generator=image
 await js(`(()=>{const s=document.querySelector('.app-shell').__vueParentComponent.setupState;s.select(${JSON.stringify(image)});s.deleteSelectedNodes();document.querySelector('.canvas-wrap').focus({preventScroll:true});return true})()`)
 await until(`document.querySelectorAll('.vue-flow__node-generator').length===0`)
 await sentinel()
 await activate()
 await js(`document.querySelector('.canvas-wrap').focus({preventScroll:true});true`)
 const before=await info();assert.equal(before.canvasHistory,2)
 await nativeKey('keystroke "z" using command down')
 const plain=await afterKey('native_after_edit_closed_sentinel',before)
 plain.canvasRestored=plain.after.generators===1
 plain.sentinelReopened=plain.tabs.some(url=>url.includes('qa-native-undo-sentinel'))
 report.checks.native_after_edit_closed_sentinel=plain
 // Return to our original QA tab even if the browser consumed Cmd+Z and reopened the sentinel.
 await jxa(`const s=Application('Safari'),w=s.windows.byId(${windowId});const target=w.tabs().find(t=>t.url()===${JSON.stringify(expectedProject)});w.currentTab=target;for(const t of w.tabs().filter(t=>t.url().includes('qa-native-undo-sentinel')))t.close();true;`)
 await asyncJs(`(async()=>{const{sequenceBranchTask}=await import('/tests/fixtures/sequenceBranches.ts');const s=document.querySelector('.app-shell').__vueParentComponent.setupState;const task=sequenceBranchTask('native-modal',1);task.jobId=null;task.generationSequences=[];task.narrativeEvents=[];task.shots.forEach(shot=>{shot.thumb='';shot.frames=[]});s.store.restoreTask(task);s.openNodeDetails(task.prompts[0].id,{taskKey:task.key});return true})()`)
 await until(`Boolean(document.querySelector('[data-canvas-details] textarea[readonly]'))`)
 await sentinel()
 await activate()
 await js(`document.querySelector('[data-canvas-details] .prompt-box-actions button:nth-of-type(2)').click();true`)
 await until(`Boolean(document.querySelector('.prompt-modal[aria-modal="true"] textarea[readonly]'))`)
 await js(`document.querySelector('.prompt-modal textarea').focus({preventScroll:true});true`)
 const menuBefore=await info()
 await nativeKey('keystroke "z" using command down')
 const closed=await afterKey('readonly_full_preview_closed_sentinel',menuBefore)
 closed.sentinelReopened=closed.tabs.some(url=>url.includes('qa-native-undo-sentinel'))
 closed.canvasHistoryUnchanged=closed.after.canvasHistory===menuBefore.canvasHistory
 report.checks.readonly_full_preview_closed_sentinel=closed
 assert.equal(plain.canvasRestored,true,'Canvas native Cmd+Z restores the temporary node')
 assert.equal(plain.sentinelReopened,false,'Canvas native Cmd+Z does not reopen a closed tab')
 assert.equal(closed.after.keys.at(-1)?.prevented,true,'Readonly full-preview native Cmd+Z is consumed')
 assert.equal(closed.canvasHistoryUnchanged,true,'Full-preview Cmd+Z does not change the underlying canvas')
 assert.equal(closed.sentinelReopened,false,'Full-preview native Cmd+Z does not reopen the sentinel')
 await js(`document.querySelector('.prompt-modal header button').click();document.querySelector('.canvas-wrap').focus({preventScroll:true});true`)
 await until(`!document.querySelector('.prompt-modal')`)
 const afterModalBefore=await info();assert.ok(afterModalBefore.canvasHistory>0)
 await nativeKey('keystroke "z" using command down')
 const afterModal=await afterKey('canvas_after_preview_closed',afterModalBefore)
 assert.equal(afterModal.after.canvasHistory,afterModalBefore.canvasHistory-1,'Closing the preview restores normal canvas Undo')
 assert.equal(afterModal.after.keys.at(-1)?.prevented,true)
 assert.equal(afterModal.tabs.some(url=>url.includes('qa-native-undo-sentinel')),false)
 assert.deepEqual(await js('window.__nativeMenuProbe.blocked'),[],'No model or generation submission was attempted')
 report.probeCompleted=true;report.passed=true
}catch(error){report.failure=String(error);try{report.last=await info()}catch(_){}throw error}
finally{await writeFile(output+'/report.json',JSON.stringify(report,null,2));if(windowId)try{await jxa(`const s=Application('Safari'),w=s.windows.byId(${windowId});if(w.tabs().every(t=>t.url().startsWith(${JSON.stringify(app)})))w.close();true;`)}catch(_){}if(previousFrontPid)try{await jxa(`ObjC.import('AppKit');const app=$.NSRunningApplication.runningApplicationWithProcessIdentifier(${previousFrontPid});if(app)app.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);true;`)}catch(_){}}
console.log(JSON.stringify({passed:report.passed,checks:Object.keys(report.checks),output}))
