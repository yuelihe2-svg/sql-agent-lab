// Exercise the actual source/mode selection code; no model or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'live_ui.html'), 'utf8');
new Function(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
assert.match(html, /<option value="custom">数据库问答<\/option>/);
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) {
    const el = {value: '', textContent: '', hidden: false};
    el.classList = {toggle: (_, value) => {el.hidden = value;}};
    elements.set(id, el);
  }
  return elements.get(id);
};
const context = vm.createContext({$, options(el, values) {el.options = values; el.value = values[0]?.[0] || '';}});
vm.runInContext('let catalog;' + html.slice(html.indexOf('function questions(){'), html.indexOf("$('start').onclick=")), context);
vm.runInContext("catalog={sources:[{id:'own',name:'独立数据'},{id:'bird',name:'BIRD'}],databases:['own','bird'],questions:[{question_id:'1',db_id:'bird',question:'Benchmark question'}]}", context);
$('mode').value = 'custom';
context.configureSources();
assert.equal($('db').options.length, 2);
assert.equal($('benchmark').hidden, true);
assert.equal($('question').readOnly, false);
$('mode').value = 'benchmark';
context.configureSources();
assert.equal($('db').value, 'bird');
assert.equal($('db').options.length, 1, 'Independent data must not appear as a BIRD database');
assert.equal($('question').value, 'Benchmark question');
assert.equal($('question').readOnly, true);
$('mode').value = 'custom';
context.configureSources();
assert.equal($('db').value, 'bird', 'Preserve a valid database selection when switching modes');
assert.equal($('question').value, '');
// An already-running older backend has only databases and questions.
vm.runInContext("catalog={databases:['legacy'],questions:[]}", context);
context.configureSources();
assert.equal($('db').value, 'legacy');
// Ordinary queries must not be described as missing benchmark evidence.
const evidenceArea = $('evaluationEvidence');
evidenceArea.replaceChildren = (...children) => {evidenceArea.children = children;};
evidenceArea.append = child => evidenceArea.children.push(child);
context.text = (tag, value) => ({tag, value});
vm.runInContext(html.slice(html.indexOf('function renderEvaluationEvidence(run){'), html.indexOf('function taskPresentation(run){')), context);
context.renderEvaluationEvidence({manifest:{benchmark:false}, result:{score:null}});
assert.match(evidenceArea.children[1].value, /没有参考答案/);
context.renderEvaluationEvidence({manifest:{benchmark:true}, result:null});
assert.match(evidenceArea.children[1].value, /运行结束后再生成评测证据/);
// Startup must offer only ordinary questions when no benchmark is configured.
context.api = async () => ({sources:[{id:'own',name:'独立数据'}], databases:['own'], questions:[], benchmark_available:false});
context.refresh = async () => {};
context.setInterval = () => {};
context.explainError = e => e;
vm.runInContext(html.slice(html.indexOf('async function init(){'), html.indexOf('</script>')), context);
setImmediate(() => {
  assert.equal($('mode').options.length, 1);
  assert.equal($('mode').value, 'custom');
  assert.equal($('db').value, 'own');
  assert.equal($('question').readOnly, false);
  console.log('Source UI checks passed');
});
