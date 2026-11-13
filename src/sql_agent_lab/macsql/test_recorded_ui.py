"""Historical API runs are a read-only view, with independent score/review states."""
import hashlib
import json
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest

from .live_ui import Observatory, make_handler


def fixture_app(tmp_path):
    app = Observatory.__new__(Observatory)
    app.output = tmp_path/'live'
    app.output.mkdir()
    app.history = None
    app.active = None
    app.stop = threading.Event()
    batch = tmp_path/'batch'
    batch.mkdir()
    app.recorded_batches = [batch]
    (batch/'protocol.json').write_text(json.dumps({'ids':['1','2','3'], 'model':'deepseek-flash',
                                                  'documented_version':'DeepSeek-V4.1-Flash'}))
    for qid, score in [('1',1),('2',0),('3',None)]:
        path=batch/('q'+qid)
        path.mkdir()
        (path/'manifest.json').write_text(json.dumps({'question':{'question_id':qid,'db_id':'db','question':'Question'},
                 'model':{'name':'deepseek-flash'},'semantic_review':True}))
        (path/'result.json').write_text(json.dumps({'score':score,'semantic_status':'unverified',
            'cloud_api_calls':2,'input_tokens':100,'output_tokens':20,'elapsed_seconds':4,
            'estimated_cost_usd_peak_no_cache':.001}))
        (path/'trace.jsonl').write_text('\n'.join(json.dumps(e) for e in [
            {'event':'call_completed','role':'refiner','response':'{"action":"accept"}',
             'purpose':'semantic_review','elapsed_seconds_since_start':3.5},
            {'event':'semantic_review_error','code':'ungrounded_requirement','error':'Invalid quote'},
        ]),encoding='utf8')
    return app, batch


def test_readonly_http_preserves_raw_records_and_unverified_review(tmp_path,monkeypatch):
    app,batch=fixture_app(tmp_path)
    def forbidden(*a,**kw):
        pytest.fail('Viewing records must not execute a workflow or evaluation')
    monkeypatch.setattr(app,'start',forbidden)
    monkeypatch.setattr(app,'work',forbidden)
    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.LocalOllamaModel',forbidden)
    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.score_sql',forbidden)
    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in batch.rglob('*') if p.is_file()}
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            index=client.get('/api/runs').json()
            summary=index['batches'][0]
            assert (summary['correct'],summary['scored'],summary['planned'],summary['accuracy'])==(1,2,3,.5)
            assert summary['totals']['cloud_api_calls']==6
            record=client.get('/api/run?id=recorded-0-q2').json()
            assert record['recorded'] and record['historical'] and not record['active']
            assert record['result']['score']==0
            assert record['result']['semantic_status']=='unverified'
            assert record['events'][0]['response']=='{"action":"accept"}'
            assert record['events'][0]['elapsed']==3.5
            assert 'elapsed' not in record['events'][1]  # Missing timing is not fabricated as zero/null.
            assert client.get('/api/run?id=../../batch').status_code==400
    finally:
        server.shutdown();server.server_close();thread.join()
    assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


def test_missing_tokens_remain_unknown(tmp_path):
    app,batch=fixture_app(tmp_path)
    result=batch/'q3/result.json'
    result.write_text('{"score": null}')
    assert app.run_index()['batches'][0]['totals']['input_tokens'] is None


def test_display_annotations_do_not_rewrite_scores_or_history(tmp_path):
    app,batch=fixture_app(tmp_path)
    result=batch/'q1/result.json'
    result.write_text(json.dumps({'score':1,'semantic_status':'output_unverified',
        'semantic_metrics':{'elapsed_seconds':3.9},'added_review_seconds':7.0,'elapsed_seconds':9.7}))
    app.analysis_dir=tmp_path/'analysis';app.analysis_dir.mkdir()
    analysis=app.analysis_dir/'analysis.json'
    analysis.write_text(json.dumps({'rows':[{'source':str(result),'score':1,'stages':{'semantic':{'status':'未检查'}}}]}))
    before={p:p.read_bytes() for p in (analysis,result)}
    shown=app.analysis_view()['rows'][0]
    assert shown['score']==1 and shown['semantic_status']=='output_unverified'
    assert shown['review_timing']==dict(requirements_seconds=None,sql_review_seconds=3.9,sql_review_ran=True,total_review_seconds=7.0,run_seconds=9.7)
    assert all(p.read_bytes()==value for p,value in before.items())


def test_evaluation_evidence_only_visible_after_result_is_saved(tmp_path):
    app,batch=fixture_app(tmp_path)
    p=batch/'q1';artifact=p/'evaluation-evidence.json'
    artifact.write_text('{"model_input": false, "comparisons": []}')
    assert app.read_run('recorded-0-q1')['evaluation_evidence']['model_input'] is False
    pending=app.output/'pending';pending.mkdir()
    (pending/'manifest.json').write_text('{"question": {"question_id":"pending"}}')
    (pending/'evaluation-evidence.json').write_text('{"model_input": false}')
    assert app.read_run('pending')['evaluation_evidence'] is None


def test_old_history_still_available(tmp_path):
    app,_=fixture_app(tmp_path)
    old=app.output/'old-run'
    old.mkdir()
    (old/'manifest.json').write_text('{"question":{"question_id":"old"}}')
    (old/'trace.jsonl').write_text('{"event":"workflow_started","elapsed":1}\n{"partial":')
    r=app.read_run('old-run')
    assert r['events']==[{'event':'workflow_started','elapsed':1}]
    assert not r['recorded'] and r['result'] is None


def test_recorded_ids_cannot_escape_registered_root(tmp_path):
    app,batch=fixture_app(tmp_path)
    (batch/'protocol.json').write_text('{"ids":["../../outside"],"model":"x"}')
    with pytest.raises(ValueError,match='Invalid recorded'):
        app.paths()
