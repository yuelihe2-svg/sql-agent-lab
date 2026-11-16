"""No-network checks for the experiment's actual agent and sampling paths."""
import sqlite3

import pytest

from sql_agent_lab.experiments.role_comparison import make_agent, sample_rows, SETTINGS
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_core import ModelReply
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.tools import SQLiteTools


@pytest.fixture
def db_tools(tmp_path):
    path=tmp_path/'fixture.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE t(n INTEGER); INSERT INTO t VALUES(42);')
    return SQLiteTools({'db':path})


def exercise(setting,tools,sqls):
    calls,events=[],[]
    def model(**kwargs):
        calls.append(kwargs)
        assert 'GOLD_SENTINEL' not in str(kwargs['messages'])
        return ModelReply('```sql\n'+sqls[len(calls)-1]+'\n```',1,1,simulated=True)
    agent=make_agent(setting,tools,model,events.append)
    sql=agent.run(Question('test','db','Return n from t'))
    return sql,calls,events


def test_a_does_not_repair_execution_error(db_tools):
    sql,calls,events=exercise('A',db_tools,['SELECT missing FROM t'])
    assert len(calls)==1
    assert sql=='SELECT missing FROM t'
    assert events[-1]['status']=='error'


def test_b_reuses_general_role_and_receives_feedback(db_tools):
    sql,calls,events=exercise('B',db_tools,['SELECT missing FROM t','SELECT n FROM t'])
    assert sql=='SELECT n FROM t'
    assert [c['role'] for c in calls]==['decomposer','decomposer']
    assert calls[0]['messages'][0]==calls[1]['messages'][0]
    assert 'no such column' in str(calls[1]['messages'])
    assert events[-1]['repair_count']==1


def test_a_and_b_have_identical_initial_prompt(db_tools):
    _,a,_=exercise('A',db_tools,['SELECT n FROM t'])
    _,b,_=exercise('B',db_tools,['SELECT n FROM t'])
    assert a[0]==b[0]


def test_c_uses_existing_agent_and_refiner(db_tools):
    agent=make_agent('C',db_tools,None,None)
    assert type(agent) is ReferenceMacSQLAgent
    sql,calls,_=exercise('C',db_tools,['SELECT missing FROM t','SELECT n FROM t'])
    assert sql=='SELECT n FROM t'
    assert [c['role'] for c in calls]==['decomposer','refiner']


@pytest.mark.parametrize('setting',SETTINGS)
def test_empty_result_does_not_trigger_model_recheck(db_tools,setting):
    _,calls,events=exercise(setting,db_tools,['SELECT n FROM t WHERE 0'])
    assert len(calls)==1
    assert events[-1]['semantic_status']=='disabled'


def test_failed_repair_is_not_repeated_or_silently_adopted(db_tools):
    sql,calls,events=exercise('B',db_tools,['SELECT missing FROM t','SELECT other_missing FROM t'])
    assert len(calls)==2 and sql=='SELECT missing FROM t'
    assert events[-1]['repair_rejected']


def test_sampling_is_paired_reproducible_and_excludes_known_cases():
    rows=[dict(question_id=i,db_id='a' if i<30 else 'b',difficulty='simple') for i in range(60)]
    sample=sample_rows(rows,{'0','1'},50,123)
    assert sample==sample_rows(rows,{'0','1'},50,123)
    assert len(sample)==len({r['question_id'] for r in sample})==50
    assert not {0,1}&{r['question_id'] for r in sample}
    assert {r['db_id'] for r in sample}=={'a','b'}


def test_reference_object_is_not_an_agent_input(db_tools):
    from sql_agent_lab.macsql.dataset import Reference
    agent=make_agent('A',db_tools,None,None)
    with pytest.raises(TypeError):
        agent.run(Reference('db','SELECT 42 AS GOLD_SENTINEL'))


def test_paired_report_excludes_unresolved_but_keeps_generation_failure():
    from sql_agent_lab.experiments.comparison_report import paired_comparison
    rows=[dict(setting=s,question_id=str(i),db_id='db',score=score)
          for s,scores in [('B',[1,0,1,None]),('A',[0,0,1,1])]
          for i,score in enumerate(scores)]
    pair=paired_comparison(rows,'B','A')
    assert pair['paired']==3 and pair['wins']==['0']
    assert pair['both_wrong']==['1'] and pair['both_correct']==['2']
    assert pair['delta']==pytest.approx(1/3)


@pytest.mark.parametrize('setting',SETTINGS)
def test_worker_records_actual_usage_and_scores_after_model(tmp_path,monkeypatch,setting):
    import json
    import httpx
    from types import SimpleNamespace
    from sql_agent_lab.experiments import role_comparison as exp
    from sql_agent_lab.macsql.deepseek_api import DeepSeekModel as ActualAdapter
    from sql_agent_lab.macsql.fetch_bird import sha256
    data=tmp_path/'data';data.mkdir()
    db=data/'db.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE t(n INTEGER); INSERT INTO t VALUES(42);')
    (data/'mini_dev_sqlite.jsonl').write_text(json.dumps(dict(question_id=1,db_id='db',
        question='Return n from t',SQL='SELECT n AS GOLD_SENTINEL FROM t')),encoding='utf8')
    (data/'source-lock.json').write_text(json.dumps({'databases':{'db':{'path':'db.sqlite'}}}),encoding='utf8')
    metadata=data/'metadata.json';metadata.write_text('{"databases": {}}',encoding='utf8')
    out=tmp_path/'results';out.mkdir()
    exp.write(out/'protocol.json',dict(ids=['1'],data=str(data),metadata=str(metadata),
        database_hashes={'db':sha256(db)},max_calls={'A':1,'B':3,'C':3}))
    def transport(request):
        payload=json.loads(request.content)
        assert 'GOLD_SENTINEL' not in str(payload)
        return httpx.Response(200,json={'id':'offline','model':'offline',
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},
            'choices':[{'message':{'content':'```sql\nSELECT n FROM t\n```'},'finish_reason':'stop'}]})
    def adapter(key,ledger,qid):
        return ActualAdapter('offline-test-placeholder',ledger,qid,transport=httpx.MockTransport(transport))
    monkeypatch.setattr(exp,'DeepSeekModel',adapter)
    exp.worker(SimpleNamespace(output=out,setting=setting,qid='1'))
    saved=json.loads((out/setting/'q1/result.json').read_text(encoding='utf8'))
    assert saved['score']==1 and saved['cloud_api_calls']==1
    assert saved['input_tokens']==10 and saved['output_tokens']==5 and saved['usage_complete']
    evidence=json.loads((out/setting/'q1/evaluation-evidence.json').read_text(encoding='utf8'))
    assert evidence['model_input'] is False


def test_chinese_report_renders_saved_results_and_escapes_question(tmp_path):
    from sql_agent_lab.experiments.role_comparison import write
    from sql_agent_lab.experiments.comparison_report import generate
    write(tmp_path/'protocol.json',dict(count_per_setting=1,model='offline',documented_version='fixture',
        seed_for_sampling=0,excluded_known_fixture_ids=[],strata_counts={'db / simple':1}))
    write(tmp_path/'questions.json',[dict(question_id=1,db_id='db',question='<script>bad</script>',difficulty='simple')])
    write(tmp_path/'completion.json',dict(database_unchanged={'db':True}))
    for setting in SETTINGS:
        folder=tmp_path/setting/'q1';folder.mkdir(parents=True)
        write(folder/'result.json',dict(setting=setting,question_id='1',db_id='db',score=1,
            status='completed',elapsed_seconds=1,agent_seconds=.5,feedback={'status':'ok'},
            cloud_api_calls=1,repair_count=0,usage_complete=True,sql='SELECT 1'))
    generate(tmp_path)
    report=(tmp_path/'report-zh.html').read_text(encoding='utf8')
    assert '<script>bad</script>' not in report and '&lt;script&gt;' in report
    assert report.count('<details>')==1
    assert '本次 B、C 均未触发修复' in report
    assert (tmp_path/'results.csv').exists()
