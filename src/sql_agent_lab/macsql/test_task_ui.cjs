// Run the real page's lifecycle and polling functions offline, without a model or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'live_ui.html'), 'utf8');
new Function(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', disabled: false, children: [],
    classList: {toggle() {}}, replaceChildren() {this.children = [];},
    append(item) {this.children.push(item);}
  });
  return elements.get(id);
}
const context = vm.createContext({$: element, Date});
vm.runInContext(html.slice(html.indexOf('function taskPresentation(run){'), html.indexOf('function render(run){')), context);
const present = value => context.taskPresentation(value);
assert.equal(present({task: {state: 'completed'}, result: {score: 0}}).label, '已完成');
assert.equal(present({task: {state: 'failed'}, result: {score: 1}}).label, '失败');
assert.equal(present({result: {score: 1}}).label, '状态未记录');
assert.equal(present({result: {status: 'needs_review', score: 1}}).label, '已完成');
assert.equal(present({active: true, task: {state: 'stopping'}}).canStop, false);
assert.equal(present({active: true, task: {state: 'running'}}).canStop, true);
context.renderTask({id: 'demo', active: true, task: {state: 'stopping', stage: 'decomposer'}});
assert.equal(element('status').textContent, '停止中');
assert.equal(element('stop').disabled, true);
assert.match(element('taskProblem').textContent, /等待当前操作/);
context.renderTask({id: 'demo', task: {state: 'interrupted', ended_at: null}});
assert.match(element('taskProblem').textContent, /准确结束时间未知/);
assert.equal(context.taskElapsed({active: false, task: {state: 'interrupted'}, manifest: {created_at: '2020-01-01'}}), '—');
context.renderTask({id: 'demo', task: {state: 'failed', error: '<b>offline failure</b>', persistence_error: 'disk full'}});
assert.match(element('taskProblem').textContent, /<b>offline failure<\/b>/); // textContent, not HTML.
assert.match(element('taskProblem').textContent, /disk full/);

async function testPolling() {
  const requests = [], rendered = [];
  let resolveList, resolveDetail, detailRequested, holdDetail = true;
  const detailSeen = new Promise(resolve => {detailRequested = resolve;});
  context.api = path => {
    requests.push(path);
    if (path === '/api/runs') return new Promise(resolve => {resolveList = resolve;});
    if (holdDetail) return new Promise(resolve => {resolveDetail = resolve; detailRequested();});
    return Promise.resolve({id: 'a', events: [], manifest: {question: {}}, result: null});
  };
  Object.assign(context, {
    renderBatches() {}, loadColumnHistory: async () => {},
    render: run => rendered.push(run.id), renderRecordExtras() {},
    explainError: value => value, text: () => ({}), answerLabel: () => '', chooseRun() {}
  });
  vm.runInContext("let current='a',active=null;" + html.slice(html.indexOf('let refreshing=false;'), html.indexOf('async function init(){')), context);
  const pending = context.refresh();
  await context.refresh();
  assert.equal(requests.length, 1, 'Overlapping polls must be skipped');
  resolveList({active: 'a', runs: [], batches: []});
  // Wait until the selected detail request has been issued, then change selection.
  await detailSeen;
  vm.runInContext("current='b'", context);
  resolveDetail({id: 'a'});
  await pending;
  assert.deepEqual(rendered, [], 'A stale detail response must not repaint the new selection');
  holdDetail = false;
  const next = context.refresh();
  resolveList({active: null, runs: [], batches: []});
  await next;
  assert.equal(requests.filter(p => p === '/api/runs').length, 2, 'Polling must unlock after completion');

  const posted = [];
  context.api = async (path, payload) => {posted.push({path, payload});};
  context.refresh = async () => {};
  vm.runInContext(html.split('\n').find(line => line.startsWith("$('stop').onclick=")), context);
  await element('stop').onclick();
  assert.equal(posted[0].path, '/api/stop');
  assert.equal(posted[0].payload.id, 'b', 'Stop must identify the selected run');
}
testPolling().then(() => console.log('Task states, stop controls, elapsed time and polling checks passed'))
  .catch(error => {console.error(error); process.exitCode = 1;});
