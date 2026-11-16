import sqlite3
import threading
import pytest
from sql_agent_lab.macsql.live_ui import ObservedTools, Stopped
from sql_agent_lab.macsql.tools import SQLiteTools
from sql_agent_lab.macsql.live_ui import make_handler
from http.server import ThreadingHTTPServer
import httpx


def test_trace_reports_actual_sql_results_and_denied_writes(tmp_path):
    db = tmp_path/'test.sqlite'
    with sqlite3.connect(db) as c:
        c.executescript('CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES (42);')
    before = db.read_bytes()
    events = []
    observed = ObservedTools(SQLiteTools({'test': db}), events.append, threading.Event())
    assert observed.compile_sql_readonly('test', 'SELECT n FROM numbers')['status'] == 'ok'
    assert observed.execute_sql_readonly('test', 'SELECT n FROM numbers')['preview_rows'] == [[42]]
    assert observed.execute_sql_readonly('test', 'DELETE FROM numbers')['status'] == 'error'
    assert db.read_bytes() == before
    assert len(events) == 6
    assert events[0]['input']['sql'] == 'SELECT n FROM numbers'
    assert events[2]['tool_call_id'] == events[3]['tool_call_id']
    assert events[3]['output']['preview_rows'] == [[42]]


def test_stop_prevents_next_tool_call(tmp_path):
    db = tmp_path/'test.sqlite'
    sqlite3.connect(db).close()
    stop = threading.Event()
    stop.set()
    events = []
    observed = ObservedTools(SQLiteTools({'test': db}), events.append, stop)
    with pytest.raises(Stopped):
        observed.load_schema('test')
    assert events == []


def test_browser_write_endpoints_reject_foreign_origin_and_invalid_body():
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(object()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            assert client.post('/api/start', json={}, headers={'Origin': 'https://example.com'}).status_code == 403
            assert client.post('/api/start', json=[]).status_code == 400
            assert client.get('/api/runs', headers={'Host': 'foreign.example'}).status_code == 403
            page = client.get('/')
            assert page.status_code == 200
            assert '让数据，回答你的问题' in page.text
            assert '详细视图' in page.text
            observatory = client.get('/observatory')
            assert observatory.status_code == 200
            assert '执行轨迹' in observatory.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_read_only_analysis_routes_and_missing_configuration(tmp_path):
    from types import SimpleNamespace
    import json
    (tmp_path/'analysis.json').write_text(json.dumps({'rows': [], 'new_model_calls':0}), encoding='utf8')
    (tmp_path/'逐题阶段分析.csv').write_text('版本,题号\nV3,308\n', encoding='utf8')
    app=SimpleNamespace(analysis_dir=tmp_path)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            assert client.get('/api/analysis').json()['new_model_calls']==0
            assert 'V3,308' in client.get('/analysis.csv').text
            page=client.get('/analysis');assert page.status_code==200 and '未检查' in page.text
            assert client.get('/api/analysis?path=../../config').json()=={'rows':[], 'new_model_calls':0}
            app.analysis_dir=None
            assert client.get('/api/analysis').status_code==404
    finally:
        server.shutdown();server.server_close();thread.join()
