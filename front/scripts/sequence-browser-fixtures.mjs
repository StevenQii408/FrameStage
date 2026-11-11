/** Vite-dev-only, in-memory fixtures. Never request or mutate backend jobs. */
export async function verifyMockBranches({ evaluate, waitFor, nodeInfo, assertNoOverlap, screenshot, assert }) {
  const cases = []
  for (const count of [1, 10, 11, 30, 53]) {
    const fixture = await evaluate(`(async()=>{
      const { sequenceBranchTask } = await import('/tests/fixtures/sequenceBranches.ts');
      const setup = document.querySelector('.app-shell').__vueParentComponent.setupState;
      const store = setup.store;
      if (!window.__fixtureCalls) {
        window.__fixtureCalls = [];
        const originalFetch = window.fetch;
        window.fetch = function(input,init) {
          const url = String(input?.url || input);
          // Project autosave is a local fixture acknowledgement, never a write
          // to the backend canvas being inspected by this browser session.
          if (url.includes('/api/projects/') && url.includes('/canvas') && init?.method === 'PUT') {
            const body = JSON.parse(init.body);
            return Promise.resolve(new Response(JSON.stringify({revision:body.expected_revision+1,state:body.state}),{status:200,headers:{'Content-Type':'application/json'}}));
          }
          if (url.includes('/api/')) { window.__fixtureCalls.push(url); return Promise.reject(new Error('Fixture forbids backend calls')); }
          return originalFetch.call(this,input,init);
        };
        window.EventSource = class { constructor(url) { window.__fixtureCalls.push(String(url)); throw new Error('Fixture forbids SSE'); } };
      }
      store.tasks = []; store.selectedId = '';
      const long = sequenceBranchTask('browser-${count}',${count});
      const other = sequenceBranchTask('browser-other-${count}',11,[6,4,1]);
      other.position = {x:80,y:650};
      for (const task of [long,other]) {
        task.jobId = null;
        task.videoFilename = '[QA mock] '+task.videoFilename;
        for (const [index,shot] of task.shots.entries()) {
          const frame = shot.frames[0];
          const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="'+frame.width+'" height="'+frame.height+'" data-fixture="'+shot.id+'"><rect width="100%" height="100%" fill="hsl('+index*31%360+' 35% 40%)"/></svg>';
          const url = 'data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
          shot.thumb = url; shot.frames.forEach(frame=>frame.url=url);
        }
        for (const sequence of task.generationSequences) {
          const duration = Math.min(sequence.source_duration_seconds,11.5);
          const ratio = duration/sequence.generation_duration_seconds;
          sequence.timeline.forEach((stage,index)=>{
            stage.generation_start_seconds = Number((stage.generation_start_seconds*ratio).toFixed(3));
            stage.generation_end_seconds = Number((stage.generation_end_seconds*ratio).toFixed(3));
            stage.semantic_phase = index ? '饮用与反馈' : '递交与接稳';
          });
          sequence.generation_duration_seconds = duration;
        }
        store.restoreTask(task);
      }
      store.selectedId = '';
      setup.buildGraph();
      window.__branchFixture = {setup,long,other};
      return {long:long.key,other:other.key,clip:long.generationSequences[0].id,
        ids:long.shots.map(shot=>shot.id),prompts:[long,other].flatMap(task=>task.generationSequences.map(sequence=>sequence.seedance_prompt.id))};
    })()`)
    await waitFor(`document.querySelectorAll('[data-testid="generation-sequence-card"]').length===4`)
    await new Promise(resolve => setTimeout(resolve, 600))
    const before = await evaluate(nodeInfo)
    assertNoOverlap(before)
    assert.equal(before.filter(node => node.type.includes('vue-flow__node-shot')).length, 0)
    const stable = before.filter(node => node.id === fixture.other || node.id.startsWith(`browser-other-${count}`))
      .map(({ id, transform }) => ({ id, transform }))
    if (count > 1) {
      await evaluate(`document.querySelector('[data-id="${fixture.clip}"] [data-testid="sequence-toggle"]').click()`)
      await waitFor(`document.querySelectorAll('.vue-flow__node-shot').length===${count}`)
      await new Promise(resolve => setTimeout(resolve, 400))
      const after = await evaluate(nodeInfo)
      assertNoOverlap(after)
      const sources = after.filter(node => fixture.ids.includes(node.id))
      assert.deepEqual(sources.map(node => node.id), fixture.ids)
      const rows = new Map()
      sources.forEach(node => {
        const y = Math.round(node.rect.y)
        rows.set(y, (rows.get(y) || 0) + 1)
      })
      assert.equal(rows.size, Math.ceil(count / 4))
      assert.ok([...rows.values()].every(size => size <= 4))
      assert.deepEqual(after.filter(node => node.id === fixture.other || node.id.startsWith(`browser-other-${count}`))
        .map(({ id, transform }) => ({ id, transform })), stable)
      for (const root of [fixture.long, fixture.other]) {
        assert.equal(after.find(node => node.id === root).transform, before.find(node => node.id === root).transform)
      }
      for (const prompt of fixture.prompts) assert.ok(after.some(node => node.id === prompt))
      if (count === 53) {
        await evaluate(`window.__branchFixture.setup.fitView({padding:.12,minZoom:.22,maxZoom:.22,duration:0})`)
        await new Promise(resolve => setTimeout(resolve, 400))
        assert.equal(await evaluate(`document.querySelectorAll('.vue-flow__node-shot').length`), 53)
        assertNoOverlap(await evaluate(nodeInfo))
        await screenshot('mock-53-overview.png')
      }
      await evaluate(`document.querySelector('[data-id="${fixture.clip}"] [data-testid="sequence-toggle"]').click()`)
      await waitFor(`document.querySelectorAll('.vue-flow__node-shot').length===0`)
    }
    assert.equal(await evaluate(`document.querySelectorAll('[data-testid="event-prompt-card"]').length`), 4)
    assert.equal(await evaluate(`window.__fixtureCalls.length`), 0)
    cases.push({ source_count: count, rows: count > 1 ? Math.ceil(count / 4) : 0, tasks: 2, passed: true })
  }
  return cases
}
