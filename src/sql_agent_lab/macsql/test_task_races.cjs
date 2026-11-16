// Delayed real page requests: an accepted action must invalidate older polls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'live_ui.html'), 'utf8');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {
    disabled: false, textContent: '', replaceChildren() {}, append() {},
    classList: {remove() {}, toggle() {}}
  });
  return elements.get(id);
};

async function testStopDuringPoll() {
  let releaseDetail, detailRequested, stopped = false;
  const detailSeen = new Promise(resolve => {detailRequested = resolve;});
  const rendered = [];
  const context = vm.createContext({
    $, renderBatches() {}, text: () => ({}), explainError: v => v,
    loadColumnHistory: async () => {}, renderRecordExtras() {},
    render: run => {rendered.push(run.task.state); $('stop').disabled = run.task.state !== 'running';},
    api: async (url, payload) => {
      if (url === '/api/stop') {
        assert.equal(payload.id, 'a');
        stopped = true;
        return {status: 'stop_requested'};
      }
      if (url === '/api/runs') return {active: 'a', runs: [], batches: []};
      if (stopped) return {id: 'a', active: true, task: {state: 'stopping'}};
      return new Promise(resolve => {releaseDetail = resolve; detailRequested();});
    }
  });
  vm.runInContext("let current='a',active=null;" + html.slice(html.indexOf('let refreshing=false;'), html.indexOf('async function init(){')), context);
  vm.runInContext(html.split('\n').find(line => line.startsWith("$('stop').onclick=")), context);
  const poll = context.refresh();
  await detailSeen;
  await $('stop').onclick();
  releaseDetail({id: 'a', active: true, task: {state: 'running'}});
  await poll;
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!rendered.includes('running'), 'An older poll overwrote the accepted stop request');
  assert.equal(rendered.at(-1), 'stopping', 'Fetch current state after discarding an obsolete poll');
  assert.equal($('stop').disabled, true);
}
testStopDuringPoll().then(() => console.log('Delayed poll after stop acceptance: passed'))
  .catch(error => {console.error(error); process.exitCode = 1;});
