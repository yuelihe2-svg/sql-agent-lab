// Real user-page logic, with a tiny DOM and controlled requests; no network/model.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'user_ui.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
function setup(){
  const elements=new Map();
  function el(tag='div'){return {tag,children:[],value:'',textContent:'',hidden:false,disabled:false,attributes:{},append(...items){this.children.push(...items);},prepend(...items){this.children.unshift(...items);},replaceChildren(...items){this.children=items;},setAttribute(k,v){this.attributes[k]=v;},focus(){}};}
  const get=id=>{if(!elements.has(id))elements.set(id,el());return elements.get(id);};
  const context=vm.createContext({document:{getElementById:get,createElement:el},location:{search:''},URLSearchParams,Date,history:{replaceState(){}},localStorage:{setItem(){},getItem(){return null;}}});
  vm.runInContext(script.slice(0,script.indexOf("$('simpleView').onclick=")),context);
  vm.runInContext("state.catalog={databases:['db'],sources:[{id:'db',name:'测试库'}]};",context);
  return {context,get};
}
function run(id='a',state='completed'){return {id,active:state==='running',task:{state,stage:'decomposer'},manifest:{question:{db_id:'db',question:'测试问题'},benchmark:false},events:[],result:state==='running'?null:{status:'completed',score:null,sql:'SELECT n FROM numbers',feedback:{status:'ok',columns:['n'],preview_rows:[[42],[null],['<img src=x onerror=alert(1)>']],has_more:true}}};}
async function checks(){
  const {context:c,get}=setup();let requests=0;c.api=async()=>{requests++;};
  c.renderRun(run());c.setView(true);c.setView(false);
  assert.equal(requests,0,'View switching must never invoke an API or model');
  assert.equal(get('detailPanel').hidden,true);
  assert.equal(get('sql').textContent,'SELECT n FROM numbers');
  assert.equal(get('tableWrap').children[0].children[1].children[2].children[1].textContent,'<img src=x onerror=alert(1)>','Database content must be literal text');
  assert.match(get('rowCount').textContent,/仅部分预览/);
  assert.equal(get('tokenUsage').textContent,'未记录','Missing usage is not zero');
  assert.equal(get('evaluation').textContent,'未评测');
  const wrong=run();wrong.result.score=0;c.renderRun(wrong);
  assert.equal(get('status').textContent,'已完成');assert.equal(get('evaluation').textContent,'评测未通过');
  const empty=run();empty.result.feedback.preview_rows=[];c.renderTable(empty.result);assert.match(get('tableWrap').children[0].textContent,/没有匹配/);
  const fail=run('failed','failed');fail.result={status:'failed',error:'no such column: demo'};c.renderRun(fail);
  assert.match(get('answerText').textContent,/未找到对应/);assert.equal(get('technicalError').hidden,false);

  // Basic answers use the real execution shape, including message (not error).
  function answerRun(rows,overrides={}){const sample=run();sample.result.feedback={status:'ok',columns:['记录总数'],column_count:1,preview_rows:rows,has_more:false,truncated:false,...overrides};return sample;}
  const single=answerRun([[17686]]);c.renderRun(single);
  assert.equal(get('answerText').textContent,'「记录总数」的查询结果是 17,686。');
  assert.match(c.basicAnswer(answerRun([[0]])).text,/是 0。/);
  assert.match(c.basicAnswer(answerRun([[null]])).text,/NULL（空值）/);
  assert.match(c.basicAnswer(answerRun([['']])).text,/空字符串/);
  assert.match(c.basicAnswer(answerRun([])).text,/没有匹配的记录/);
  assert.match(c.basicAnswer(answerRun([[0.0000001]])).text,/0\.0000001/,'Small numbers must not round to zero');
  assert.match(c.basicAnswer(answerRun([['00123']])).text,/00123/,'Text identifiers retain leading zeros');
  for(const feedback of [{has_more:true},{truncated:true},{has_more:undefined,truncated:undefined}]){
    assert.match(c.basicAnswer(answerRun([[42]],feedback)).text,/当前预览/);
    assert.match(c.basicAnswer(answerRun([],feedback)).text,/当前预览/,'A truncated zero-row preview is not an empty result');
  }
  assert.match(c.basicAnswer(answerRun([[42]],{column_count:3})).text,/1 行/,'A single visible column is not necessarily a scalar');
  assert.match(c.basicAnswer(answerRun([[1],[2]])).text,/2 行、1 列/);
  const review=answerRun([[1]]);review.result.status='needs_review';assert.match(c.basicAnswer(review).text,/仍有待核对/);
  const escaped=answerRun([['<img src=x onerror=alert(1)>']]);c.renderRun(escaped);
  assert.equal(get('answerText').textContent,'「记录总数」的查询结果是 <img src=x onerror=alert(1)>。');
  const sqlFailure=run('bad','failed');sqlFailure.result={status:'failed',sql:'SELECT missing FROM numbers',feedback:{status:'error',message:'no such column: missing'}};c.renderRun(sqlFailure);
  assert.match(get('answerText').textContent,/未找到对应/);assert.equal(get('rawError').textContent,'no such column: missing');
  assert.equal(get('sql').textContent,'SELECT missing FROM numbers');
  assert.match(c.friendlyError('near "FROM": syntax error'),/语法/);
  assert.match(c.friendlyError('not authorized'),/只读检查/);
  assert.match(c.friendlyError('unable to open database file'),/数据源/);
  assert.match(c.friendlyError('Connection refused'),/模型服务/);
  const saveFailure=answerRun([[42]]);saveFailure.task.state='failed';assert.match(c.basicAnswer(saveFailure).text,/SQL 已执行，但任务未正常完成/);
  for(const status of ['running','stopping','stopped','interrupted']){const transient=answerRun([[42]]);transient.task.state=status;assert.equal(c.basicAnswer(transient),null);}
  c.renderRun(run('pending','running'));assert.equal(get('basicAnswer').hidden,true);assert.equal(get('answerText').textContent,'');
  assert.equal(requests,0,'All answer formatting must use existing data only');

  // If selection changes while an old detail request is in flight, only the new one renders.
  let release,entered;const seen=new Promise(r=>{entered=r;});const rendered=[];
  c.renderRun=value=>rendered.push(value.id);
  c.api=async url=>{if(url==='/api/runs')return {active:null,runs:[]};if(url.endsWith('=a'))return new Promise(resolve=>{release=resolve;entered();});return run('b');};
  c.setSelection('a');const first=c.refresh();await seen;c.setSelection('b');release(run('a'));await first;await new Promise(setImmediate);
  assert.deepEqual(rendered,['b']);

  // Submitting once, even with a double click, keeps the workflow options unchanged.
  const {context:d,get:dg}=setup();let releaseStart;const posted=[];
  d.api=async(url,payload)=>{posted.push({url,payload});return new Promise(resolve=>{releaseStart=resolve;});};d.refresh=async()=>{};
  dg('source').value='db';dg('question').value='统计记录';const start=d.startQuery();await d.startQuery();
  assert.equal(posted.length,1);assert.deepEqual(JSON.parse(JSON.stringify(posted[0].payload)),{db_id:'db',question:'统计记录',retrieval:true,semantic_review:false});
  releaseStart({id:'new'});await start;
  vm.runInContext('state.active=null;',d);dg('question').value='统计【表名】';await d.startQuery();assert.equal(posted.length,1);assert.match(dg('requestError').textContent,/替换/);

  // A delayed poll cannot restore "running" after an accepted stop request.
  const {context:e}=setup();let oldDetail,detailEntered,stopped=false;const detailSeen=new Promise(r=>{detailEntered=r;});const states=[];
  e.setSelection('stop-me');vm.runInContext("state.run={active:true,task:{state:'running'}};",e);
  e.renderRun=value=>states.push(value.task.state);
  e.api=async(url,payload)=>{if(url==='/api/stop'){assert.equal(payload.id,'stop-me');stopped=true;return {status:'stop_requested'};}if(url==='/api/runs')return {active:'stop-me',runs:[]};if(stopped)return {id:'stop-me',task:{state:'stopping'}};return new Promise(resolve=>{oldDetail=resolve;detailEntered();});};
  const poll=e.refresh();await detailSeen;await e.stopQuery();oldDetail({id:'stop-me',task:{state:'running'}});await poll;await new Promise(setImmediate);
  assert.deepEqual(states,['stopping']);
  console.log('User UI: view switching, literal results, empty/error states, usage, submit/stop and poll races passed');
}
checks().catch(error=>{console.error(error);process.exitCode=1;});
