// Offline checks of the actual page classifiers. No browser/network/model calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, name + '.html'), 'utf8');
const pages = {analysis: read('analysis'), live_ui: read('live_ui')};
for (const html of Object.values(pages)) new Function(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
const helper = html => html.match(/function reviewFailureLabel\(error\)\{[\s\S]*?\n\}/)[0];
assert.equal(helper(pages.analysis), helper(pages.live_ui));
const context = vm.createContext({});
const stage = pages.analysis.slice(pages.analysis.indexOf('function stageInfo(row,key){'), pages.analysis.indexOf('function answerInfo(row){'));
const answer = pages.analysis.match(/function answerInfo\(row\)\{[^\n]+/)[0];
vm.runInContext(`const cls={'失败':'fail','未检查':'unknown'};let data={evidence:{}};${helper(pages.analysis)}\n${stage}\n${answer}`, context);
vm.runInContext(`
data.evidence.e={excerpt:JSON.stringify({event:'semantic_review_error',code:'requirement_coverage_gap'})};
let row={score:1,categories:[],stages:{semantic:{status:'失败',detail:'old coverage error',evidence:['e']}}};
if(stageInfo(row,'semantic').label!=='复核报告校验失败'||answerInfo(row).label!=='正确')throw Error('Conflated protocol and score');
row.score=0;
if(stageInfo(row,'semantic').label!=='复核报告校验失败'||answerInfo(row).label!=='错误')throw Error('Score overwritten');
row.stages.semantic.evidence=[];row.categories=['复核漏检'];
if(stageInfo(row,'semantic').label!=='复核漏检')throw Error('Missed review false acceptance');
if(reviewFailureLabel({error_type:'TimeoutError'})!=='复核运行异常')throw Error('Runtime mislabeled as protocol');
if(reviewFailureLabel({failure_kind:'report_validation'})!=='复核报告校验失败')throw Error('New event unsupported');
row.score=null;if(answerInfo(row).label!=='无评测记录')throw Error('Invented missing score');
`, context);
console.log('UI syntax and 6 independent status/score checks passed');
const tableFunction = html => html.slice(html.indexOf('function outputColumnsTable(audit){'), html.indexOf('\n}', html.indexOf('function outputColumnsTable(audit){'))+2);
assert.equal(tableFunction(pages.analysis), tableFunction(pages.live_ui));
vm.runInContext(`
const document={createElement:()=>({textContent:'',children:[],append(...items){this.children.push(...items)}})};
${tableFunction(pages.analysis)}
const audit={status:'not_checked',columns:[{position:1,expression:'player_name'},{position:2,expression:'player_api_id'},{position:3,expression:'AVG(score) AS mean'}]};
const rendered=JSON.stringify(outputColumnsTable(audit));
if(!rendered.includes('player_api_id')||!rendered.includes('未逐列检查')||rendered.includes('需要保留'))throw Error('Historical columns must not invent dispositions');
row.output_columns_audit=audit;
if(stageInfo(row,'output_columns').label!=='未逐列检查')throw Error('Analysis column state incorrect');
`,context);
console.log('2 per-column display checks passed');
const reqFunction = html => html.slice(html.indexOf('function requirementsView(info){'),html.indexOf('\n}',html.indexOf('function requirementsView(info){'))+2);
assert.equal(reqFunction(pages.analysis),reqFunction(pages.live_ui));
vm.runInContext(`${reqFunction(pages.analysis)}
if(!JSON.stringify(requirementsView(null)).includes('没有独立提取记录'))throw Error('Legacy run inferred to be independent');
const frozen={candidate_sql_visible:false,digest:'test-hash',report:{status:'clear',requested:[{id:'o1',description:'name',source:'question',quote:'return name'}],constraints:[],ambiguities:[]}};
const renderedRequirements=JSON.stringify(requirementsView(frozen));
if(!renderedRequirements.includes('test-hash')||!renderedRequirements.includes('没有候选 SQL'))throw Error('Missing frozen requirement provenance');
`,context);
console.log('2 independent-step display checks passed');
vm.runInContext(`
frozen.assessment={risks:[{code:'shared_constraint_output_quote',output:'o1'}]};
frozen.report.requested[0].kind='attribute';frozen.report.requested[0].support='explicit';
const safetyView=JSON.stringify(requirementsView(frozen));
if(!safetyView.includes('保留原 SQL')||!safetyView.includes('明确要求'))throw Error('Output support risks must be visible');
`,context);
assert.ok(pages.live_ui.includes("semanticLabels.projection_suggested='已保存删列建议，未采用、未执行；最终仍提交原 SQL'"));
assert.ok(pages.live_ui.includes('以下仅为删列建议，未执行、未采用、未评分；最终答案保留原 SQL。'));
console.log('3 suggestion-only and support-risk display checks passed');
const suggestionHelper=html=>html.match(/function suggestionOutcome\(e\)\{[\s\S]*?\n\}/)[0];
assert.equal(suggestionHelper(pages.analysis),suggestionHelper(pages.live_ui));
vm.runInContext(`${suggestionHelper(pages.analysis)}
if(!suggestionOutcome({status:'not_generated',score:null}).includes('未生成'))throw Error('Absent proposal must not be scored');
if(!suggestionOutcome({status:'scored',score:0}).includes('错误'))throw Error('Wrong suggestion hidden');
if(!suggestionOutcome({status:'scored',score:1}).includes('正确'))throw Error('Correct suggestion hidden');
if(!suggestionOutcome({status:'scored',score:null}).includes('尚未完成'))throw Error('Unknown score invented');
`,context);
console.log('4 independent suggestion-score checks passed');
vm.runInContext(`
frozen.report.status='ambiguous';
frozen.protocol_warnings=['clear_with_ambiguities_normalized'];
const ambiguityView=JSON.stringify(requirementsView(frozen));
if(ambiguityView.includes('跳过 SQL 复核')||!ambiguityView.includes('是否继续复核请查看后续事件'))throw Error('Ambiguity cannot imply skipped inspection');
if(!ambiguityView.includes('已保留歧义并转为待核实')||!ambiguityView.includes('不能授权删列'))throw Error('Normalization and removal guard must remain visible');
`,context);
assert.ok(pages.live_ui.includes("semantic_review_scope_warning:'保留输出歧义，继续检查 SQL'"));
console.log('3 ambiguity continuation and guard display checks passed');
const realTraces=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/trace-observability-v8-real.json'),'utf8'));
context.realTraces=realTraces;
vm.runInContext(`
for(const trace of realTraces){
 row={score:trace.result.score,semantic_status:trace.result.semantic_status,categories:[],stages:{semantic:{status:'未检查',detail:'saved',evidence:[]}}};
 const err=trace.events.find(e=>e.event==='semantic_review_error');
 if(err){data.evidence.real={excerpt:JSON.stringify(err)};row.stages.semantic.evidence=['real'];}
 const label=stageInfo(row,'semantic').label;
 if(trace.question_id==='165'&&(label!=='已复核：结论待定'||answerInfo(row).label!=='错误'))throw Error('165 conflated inspection with correctness');
 if(trace.question_id==='333'&&(label!=='复核报告校验失败'||answerInfo(row).label!=='正确'))throw Error('333 invalid report changed correct answer');
 const m=trace.result.semantic_metrics;
 const timing=reviewTimingText({sql_review_seconds:m.elapsed_seconds,total_review_seconds:trace.result.added_review_seconds,run_seconds:trace.result.elapsed_seconds});
 if(!timing.includes('要求提取 未记录')||!timing.includes('SQL 复核 '+m.elapsed_seconds.toFixed(2)))throw Error('Historical timing mislabeled or invented');
}
if(reviewOutcomeLabel('accepted_with_warnings')!=='已复核：有警告')throw Error('Warning hidden');
`,context);
assert.ok(pages.live_ui.includes('此历史记录没有保存完整结果差异摘要；未重新执行 SQL 补算。'));
assert.ok(!pages.live_ui.includes('；新增复核耗时 '));
assert.ok(pages.live_ui.includes('独立评测证据（不进入模型上下文）'));
console.log('8 saved-trace state, timing and evaluation-evidence checks passed');

// Real extraction-failure trace: its elapsed_seconds belongs to requirements.
const saved=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/trace_budget_schema_20261111.json'),'utf8'));
const timingData=html=>html.match(/function reviewTimingData\(result=\{\},events=\[\]\)\{[\s\S]*?\n\}/)[0];
assert.equal(timingData(pages.analysis),timingData(pages.live_ui));
const timingHelper=html=>html.match(/function reviewTimingText\(t\)\{[\s\S]*?\n\}/)[0];
const timingContext=vm.createContext({saved});
vm.runInContext(`${timingData(pages.live_ui)}
${timingHelper(pages.live_ui)}
const t=reviewTimingData(saved['472'].result,saved['472'].events);
if(t.sql_review_seconds!==null||t.sql_review_ran!==false)throw Error('Extraction failure shown as SQL review');
if(t.requirements_seconds!==saved['472'].result.semantic_metrics.elapsed_seconds)throw Error('Lost extraction timing');
if(!reviewTimingText(t).includes('SQL 复核 未运行'))throw Error('Missing not-run label');
const unknown=reviewTimingData({semantic_metrics:{elapsed_seconds:9},elapsed_seconds:100},[]);
if(unknown.sql_review_seconds!=null||unknown.requirements_seconds!=null)throw Error('Invented historical timing');
`,timingContext);
console.log('4 real extraction-failure/unknown timing checks passed');
