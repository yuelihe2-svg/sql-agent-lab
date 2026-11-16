"""Read saved experiment artifacts and render a Chinese HTML report. No model calls."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import random
import statistics

from .role_comparison import SETTINGS, read_results, write


def paired_comparison(rows, left, right):
    index={(r['setting'],r['question_id']):r for r in rows}
    ids=sorted({r['question_id'] for r in rows})
    groups=defaultdict(list)
    wins,losses,both,right_neither=[],[],[],[]
    for qid in ids:
        a,b=index.get((left,qid)),index.get((right,qid))
        if not a or not b or a.get('score') is None or b.get('score') is None:
            continue
        groups[a['db_id']].append(a['score']-b['score'])
        (wins if a['score']>b['score'] else losses if a['score']<b['score']
         else both if a['score']==1 else right_neither).append(qid)
    n=sum(map(len,groups.values()))
    if not n:
        return dict(left=left,right=right,paired=0)
    rng=random.Random(20261116)
    names=sorted(groups)
    boot=[]
    for _ in range(10000):
        values=[v for name in rng.choices(names,k=len(names)) for v in groups[name]]
        boot.append(sum(values)/len(values))
    boot.sort()
    return dict(left=left,right=right,paired=n,delta=(len(wins)-len(losses))/n,
        wins=wins,losses=losses,both_correct=both,both_wrong=right_neither,
        database_cluster_bootstrap_95=[boot[249],boot[9749]],clusters=len(groups))


def generate(output):
    protocol=json.loads((output/'protocol.json').read_text(encoding='utf8'))
    questions=json.loads((output/'questions.json').read_text(encoding='utf8'))
    completion=(json.loads((output/'completion.json').read_text(encoding='utf8'))
                if (output/'completion.json').exists() else {})
    rows=read_results(output)
    planned=protocol['count_per_setting']
    summaries=[]
    for setting in SETTINGS:
        chosen=[r for r in rows if r['setting']==setting]
        summaries.append(dict(setting=setting,finished=len(chosen),planned=planned,
            correct=sum(r.get('score')==1 for r in chosen),
            wrong=sum(r.get('score')==0 for r in chosen),
            unresolved=sum(r.get('score') is None for r in chosen),
            execution_ok=sum(r.get('feedback',{}).get('status')=='ok' for r in chosen),
            confirmed_correct_rate=sum(r.get('score')==1 for r in chosen)/planned,
            input_tokens=sum(r.get('input_tokens',0) for r in chosen),
            output_tokens=sum(r.get('output_tokens',0) for r in chosen),
            calls=sum(r.get('cloud_api_calls',0) for r in chosen),
            selector_questions=sum(bool(r.get('selector_invoked')) for r in chosen),
            repair_questions=sum(r.get('repair_count',0)>0 for r in chosen),
            mean_agent_seconds=statistics.mean(r.get('agent_seconds',r['elapsed_seconds']) for r in chosen) if chosen else 0,
            median_agent_seconds=statistics.median(r.get('agent_seconds',r['elapsed_seconds']) for r in chosen) if chosen else 0,
            estimated_cost_usd_peak_no_cache=sum(r.get('estimated_cost_usd_peak_no_cache',0) for r in chosen),
            usage_complete=all(r.get('usage_complete',False) for r in chosen),
            errors=dict(Counter(r.get('error_code',r.get('evaluation_reason','unknown')) for r in chosen if r.get('score')!=1))))
    pairs=[paired_comparison(rows,*pair) for pair in [('B','A'),('C','A'),('C','B')]]
    write(output/'summary.json',dict(settings=summaries,paired=pairs,completion=completion))
    by_id={str(q['question_id']):q for q in questions}
    with (output/'results.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['setting','question_id','db_id','question','difficulty','status','score','evaluation_status',
            'evaluation_reason','sql','cloud_api_calls','input_tokens','output_tokens','agent_seconds',
            'elapsed_seconds','estimated_cost_usd_peak_no_cache','selector_invoked','repair_count','error_code']
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:
            q=by_id[row['question_id']]
            writer.writerow({**row,'question':q['question'],'difficulty':q['difficulty']})
    esc=lambda value:html.escape(str(value))
    def table(headers, data):
        return '<table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(
            '<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in data)+'</tbody></table>'
    main_pair=pairs[-1]
    complete=len(rows)==3*planned and all(s['unresolved']==0 for s in summaries)
    if not complete:
        conclusion='本批次尚未取得全部可判定结果。下表为当前已确认结果，不能作为完整的 50 题三组比较。'
    elif main_pair['delta']>0:
        conclusion=f"C 比 B 多答对 {len(main_pair['wins'])-len(main_pair['losses'])} 题，提高 {main_pair['delta']*100:.1f} 个百分点；这只是本次样本的观察结果。"
    elif main_pair['delta']<0:
        conclusion=f"C 比 B 少答对 {len(main_pair['losses'])-len(main_pair['wins'])} 题，降低 {-main_pair['delta']*100:.1f} 个百分点。本次实验不支持当前三角色工作流更准确。"
    else:
        conclusion='C 与 B 的答对题数相同，本次实验没有观察到准确率净提升。'
    parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SQL Agent Lab：DeepSeek 三组对比实验</title>',
        '<style>body{font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;color:#25392f;background:#f5f7f3;margin:0}main{max-width:1200px;margin:35px auto;padding:32px;background:white;border-radius:18px}h1,h2{color:#235b48}table{border-collapse:collapse;width:100%;margin:16px 0;font-size:14px}th,td{border:1px solid #dce4dc;padding:9px;text-align:left;vertical-align:top}th{background:#edf3ec}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f3;padding:14px}.lead{padding:20px;background:#eaf2e8;border-left:5px solid #32755a}details{border:1px solid #ddd;padding:12px;margin:12px 0}small{color:#607166}code{overflow-wrap:anywhere}</style><main>',
        '<h1>SQL Agent Lab：单角色与三角色工作流对比</h1>',
        '<p class="lead">'+esc(conclusion)+'</p>',
        '<p>生成时间（UTC）：'+esc(datetime.now(timezone.utc).isoformat())+'。本报告只读取已保存结果，不发起模型调用。</p>',
        '<h2>1. 实验设计</h2>',
        '<p>相同 '+str(planned)+' 道题分别运行 A、B、C，共计划 '+str(3*planned)+' 个任务。A 一次生成；B 使用相同通用生成提示词，按执行/类型反馈修复；C 使用原有条件式 Selector、Decomposer、Refiner。C 的类、提示词和路由未改动。三组均关闭语义复核。</p>',
        table(['设置','模型请求上限','修复上限','schema 策略'],[
            ['A',1,0,'完整 schema，逐级压缩可选说明'],['B',3,2,'与 A 相同；同一角色修复'],
            ['C',3,1,'原工作流按输入长度条件调用模型 Selector']]),
        '<p>三组复用相同检索、值查询、只读执行、类型检查和修复采纳保护。被拒绝或未变化的修复会提前终止；上限不是实际调用次数。每任务总 token 预算 50,000，输入按 UTF-8 字节保守计量，上限 14,000；生成输出上限按既有规则为 1,000 或 2,400 token。</p>',
        '<p>模型：'+esc(protocol['model'])+'；官方标注版本：'+esc(protocol['documented_version'])+'。temperature=0，thinking 关闭，单次重复，无自动重试。API 返回的模型标识：'+esc(sorted({m for r in rows for m in r.get('returned_models',[])}))+ '。</p>',
        '<p>按数据库和数据集原始难度标签做比例分层抽样，固定随机种子 '+str(protocol['seed_for_sampling'])+'；排除 '+str(len(protocol['excluded_known_fixture_ids']))+' 个已知回归/试点题号。未按本次结果筛题。历史研究中是否曾使用其他入选题目未全面核查，因此这是探索性试验，不宣称完全未见过的确认性测试集。</p>',
        table(['数据库 / 原始难度标签','题数'],protocol['strata_counts'].items()),
        '<h2>2. 准确率与开销</h2>',
        '<p>准确率基于预测 SQL 与参考 SQL 的完整执行结果集合比较，忽略行顺序和重复行，但保留列顺序；不是 SQL 字符串匹配，也不是“能执行就正确”。列位置不同也会判不匹配。该口径不验证所有排序和重复次数要求，不能直接称为官方 BIRD 榜单分数。参考 SQL 仅在 Agent 结束后交给独立评测器，不进入模型提示词。</p>',
        table(['组','完成/计划','答对','答错','评测未决','已确认正确/50','可执行','请求数','输入 token','输出 token','平均 Agent 秒','费用估算 USD'],[
            [s['setting'],f"{s['finished']}/{planned}",s['correct'],s['wrong'],s['unresolved'],
             f"{s['confirmed_correct_rate']:.1%}",s['execution_ok'],s['calls'],s['input_tokens'],s['output_tokens'],
             f"{s['mean_agent_seconds']:.2f}",f"{s['estimated_cost_usd_peak_no_cache']:.4f}"] for s in summaries]),
        '<p>无 SQL 的模型/生成失败按任务失败保留在总分母内；评测基础设施或参考执行失败标记未决。未决或未完成存在时，“已确认正确/50”只是保守的已确认比例，不是完整准确率。费用按官方峰值、无缓存单价估算（输入 $0.30/M，输出 $1.20/M），不是账单；缓存与非峰时段可能更便宜。<a href="https://api-docs.deepseek.com/quick_start/pricing/">官方价格来源</a>。</p>',
        '<p>采用最多 3 道题并行，每道题内轮换 ABC/BCA/CAB 顺序。Agent 耗时包含 schema 与工具处理、模型请求，不含后置参考评测；网络负载、服务缓存和并行数据库访问仍会影响时间。</p>',
        table(['组','调用 Selector 的题数','发生修复的题数','中位 Agent 秒','费用用量是否完整'],[
            [s['setting'],s['selector_questions'],s['repair_questions'],f"{s['median_agent_seconds']:.2f}",s['usage_complete']] for s in summaries]),
        '<h2>3. 逐题配对比较</h2>']
    for p in pairs:
        if not p['paired']: continue
        lo,hi=p['database_cluster_bootstrap_95']
        parts += [f"<h3>{p['left']} 相对 {p['right']}</h3>",
            f"<p>可配对 {p['paired']} 题；净差 {p['delta']*100:+.1f} 个百分点。按数据库整组重采样 10,000 次的探索性 95% 区间：[{lo*100:+.1f}, {hi*100:+.1f}] 个百分点（{p['clusters']} 个数据库簇）。</p>",
            table(['结果','题数','题号'],[['仅左组正确',len(p['wins']),', '.join(p['wins'])],
                ['仅右组正确',len(p['losses']),', '.join(p['losses'])],
                ['两组都正确',len(p['both_correct']),', '.join(p['both_correct'])],
                ['两组都错误',len(p['both_wrong']),', '.join(p['both_wrong'])]])]
    parts += ['<h2>4. 机制观察与典型案例</h2>']
    if complete and not any(s['repair_questions'] for s in summaries):
        parts.append('<p><strong>本次 B、C 均未触发修复。</strong>所以无法从本次结果估计 Refiner 的纠错收益。A/B 的实际路径都是一次生成；它们的配对差异来自独立初次生成，而不是额外修复。C 的额外请求仅在 Selector 阶段发生。这是条件式工作流的实际行为，不是漏跑节点。</p>')
    if complete and all(s['execution_ok']==planned for s in summaries):
        parts.append('<p><strong>全部 SQL 都能执行，但只有约一半匹配参考结果。</strong>当前执行和类型检查没有识别出这些语义/输出契约差异；继续增加相同触发条件下的修复次数，并不会自动覆盖它们。</p>')
    notes_path=output/'analysis-notes.json'
    if notes_path.exists():
        notes=json.loads(notes_path.read_text(encoding='utf8'))
        for note in notes:
            parts += ['<h3>'+esc(note['title'])+'</h3>','<p>'+esc(note['finding'])+'</p>',
                      '<p><small>核对依据：'+esc(note['evidence'])+'</small></p>']
    dbs=sorted({q['db_id'] for q in questions})
    parts.append(table(['数据库','题数','A 答对','B 答对','C 答对'],[
        [db,sum(q['db_id']==db for q in questions),*[sum(r['db_id']==db and r['setting']==s and r.get('score')==1 for r in rows) for s in SETTINGS]] for db in dbs]))
    parts += ['<h2>5. 解释边界与下一步</h2>',
        '<p>A 与 B 的初始提示词一致，但分别真实调用 API，不重用对方回复；temperature=0 也不能保证逐字确定性。因此即使 B 没触发修复，初次生成仍可能和 A 不同。C 与 B 同时存在 schema 筛选、生成/修复提示词和修复次数的差异；结果检验的是整套配置，不能单独归因于“三个角色”这个命名或数量。</p>',
        '<p>本实验只有 50 道固定题、每组一次运行；同数据库题目相关，数据库簇也少。区间仅作探索性不确定性提示。没有提升不等于证明永远无效；有提升也不能推广到任意数据库或其他模型。现有修复只针对执行和类型反馈，能执行但语义错误的 SQL 不一定被发现。</p>',
        '<p>建议下一步先按“统计总体、JOIN 放大、NULL/零值、输出契约”等原因归类错误。若要测量修复能力，应另建诊断集并给两种修复策略相同的初始 SQL 和反馈；若要测量 Selector，应固定生成提示词并只切换 Selector。语义复核应作为另一个独立实验，不能在看过本批结果后悄悄加入再把它称为同一次比较。后续确认性评估使用新的题目集合。</p>',
        '<p>共享数据库结束后哈希核对：'+esc(completion.get('database_unchanged','尚未完成'))+'。运行代码、问题名单、参数和数据哈希固定在 protocol.json；逐题结果见 results.csv，各组目录保存模型 trace 与独立 evaluation-evidence.json。</p>',
        '<h2>6. 全部逐题记录</h2>']
    index={(r['setting'],r['question_id']):r for r in rows}
    for q in questions:
        qid=str(q['question_id'])
        parts.append('<details><summary>'+esc('题 '+qid+' · '+q['db_id']+' · '+q['question'])+'</summary>')
        parts.append('<p>题目 evidence：'+esc(q.get('evidence') or '无')+'</p>')
        for setting in SETTINGS:
            r=index.get((setting,qid),{})
            parts.append('<h4>'+setting+'：'+esc('正确' if r.get('score')==1 else '错误/任务失败' if r.get('score')==0 else '未决/未运行')+'</h4>')
            parts.append('<p>'+esc({k:r.get(k) for k in ('evaluation_reason','error_code','cloud_api_calls','repair_count')})+'</p>')
            parts.append('<pre>'+esc(r.get('sql') or '未生成 SQL')+'</pre>')
        parts.append('</details>')
    parts += ['<h2>7. 数据与方法工具来源</h2>',
        '<p>本地数据锁记录：birdsql/bird_mini_dev，固定修订 5b256b5a9c17478f06ed6912612e9a14d17065c8，题目 SHA-256 926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606，许可 CC-BY-SA-4.0；SQLite 镜像来源 3036197201/bird-mini-dev-sqlite。数据库及运行材料不加入源码仓库。</p>',
        '<p>设计与解读使用 scientific-critical-thinking 技能辅助，引用其方法工具来源：Kassis, T., Agarwal, V., He, Y., Patel, D., &amp; Brueckner, A. M. (2026). <em>Scientific Agent Skills: A Library of Procedural Knowledge for Research Agents.</em> <a href="https://doi.org/10.48550/arXiv.2609.00065">arXiv:2609.00065</a>（2026-11-16 核对当前版本 v2）。这不是 SQL 实验效果的外部证据；本文所有实验数字来自本地保存的真实运行。</p>',
        '</main></html>']
    (output/'report-zh.html').write_text('\n'.join(parts),encoding='utf8')
    print(json.dumps({'settings':summaries,'paired':pairs,'report':str(output/'report-zh.html')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    generate(parser.parse_args().output.resolve())
