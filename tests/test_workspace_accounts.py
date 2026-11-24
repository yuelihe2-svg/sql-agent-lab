"""Offline HTTP authorization and conversation deletion regression."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import threading
import time
from uuid import uuid4

import httpx
import pytest

from sql_agent_lab.macsql.live_ui import Observatory
from test_conversations_api import build, client_for, wait_done


def wait_idle(app):
    deadline = time.monotonic() + 5
    while app.active is not None and time.monotonic() < deadline:
        time.sleep(.01)
    assert app.active is None


def test_two_accounts_cannot_read_stop_export_or_delete_each_others_conversations(tmp_path, monkeypatch):
    base = build(tmp_path, monkeypatch)
    config = tmp_path / 'accounts.json'
    alice_id, bob_id = str(uuid4()), str(uuid4())
    alice_token, bob_token = 'alpha-' * 8, 'bravo-' * 8
    config.write_text(json.dumps([
        {'id': alice_id, 'name': 'Alice', 'sources': ['shop'],
         'token_sha256': sha256(alice_token.encode()).hexdigest()},
        {'id': bob_id, 'name': 'Bob', 'sources': ['archive', 'shop'],
         'token_sha256': sha256(bob_token.encode()).hexdigest()},
    ]), encoding='utf-8')
    args = (None, base.output)
    options = dict(workspace_db=base.workspace_db, sources=tmp_path/'sources.json')
    app = Observatory(*args, **options, accounts_config=config)
    with pytest.raises(ValueError, match='requires --accounts-config'):
        Observatory(*args, **options)

    with client_for(app) as alice, httpx.Client(base_url=alice.base_url, trust_env=False) as bob:
        assert '登录 SQL Agent Studio' in alice.get('/workspace/').text
        assert alice.get('/api/catalog').status_code == 401
        assert alice.post('/api/session', json={'token': 'invalid'}).status_code == 401
        signed = alice.post('/api/session', json={'token': alice_token})
        assert signed.is_success and 'HttpOnly' in signed.headers['set-cookie']
        assert bob.post('/api/session', json={'token': bob_token}).is_success
        assert [s['id'] for s in alice.get('/api/catalog').json()['sources']] == ['shop']
        assert [s['id'] for s in bob.get('/api/catalog').json()['sources']] == ['archive', 'shop']
        assert alice.get('/api/sources/archive/profile').status_code == 404
        assert alice.get('/api/sources/shop/profile').is_success
        assert alice.post('/api/conversations', json={'datasource_id': 'archive'}).status_code == 404
        assert alice.post('/api/start', json={'db_id': 'archive', 'question': 'forbidden'}).status_code == 404
        assert alice.put('/api/sources/shop/knowledge/some-id', json={}).status_code == 404

        def create(client, source, title):
            response = client.post('/api/conversations', json={'datasource_id': source, 'title': title})
            assert response.is_success, response.text
            return response.json()['id']

        first = create(alice, 'shop', 'Alice first')
        keep = create(alice, 'shop', 'Alice keep')
        second = create(alice, 'shop', 'Alice second')
        assert bob.get('/api/sources/shop/profile').is_success
        assert bob.get('/api/conversations/'+first).status_code == 404
        assert bob.delete('/api/conversations/'+first).status_code == 404
        other = create(bob, 'archive', 'Bob private')
        path = '/api/conversations/' + other
        started = bob.post(path + '/messages', json={'question': 'Read archive'})
        assert started.is_success, started.text
        wait_idle(app)
        rid = started.json()['id']
        assert bob.get(path).json()['runs'][0]['result']['rows'] == [[7]]
        assert alice.get(path).status_code == 404
        assert alice.post(path + '/messages', json={'question': 'hidden'}).status_code == 404
        assert alice.post('/api/stop', json={'id': rid}).status_code == 404
        for route in ['/api/runs', '/api/user/runs', '/api/run?id='+rid,
                      '/api/user/run?id='+rid, '/analysis.csv', '/observatory']:
            assert alice.get(route).status_code == 404, route

        rejected = alice.post('/api/conversations/delete', json={'ids': [first, other]})
        assert rejected.status_code == 404
        assert alice.get('/api/conversations/'+first).is_success
        assert bob.get(path).is_success
        assert alice.post('/api/conversations/delete', json={'ids':[first, second]}).json()['deleted'] == [first, second]
        assert {r['id'] for r in alice.get('/api/conversations').json()['conversations']} == {keep}
        assert alice.get('/api/conversations/'+second).status_code == 404
        assert bob.get(path).is_success
        assert alice.delete('/api/session').is_success
        assert alice.get('/api/conversations').status_code == 401

    with closing(sqlite3.connect(base.workspace_db)) as conn:
        rows = conn.execute('SELECT actor_user_id, action, object_id, outcome FROM audit_event').fetchall()
        assert (bob_id, 'query', 'archive', 'ok') in rows
        assert (alice_id, 'access', other, 'denied') in rows
        assert (alice_id, 'delete', first, 'ok') in rows
        assert (alice_id, 'delete', second, 'ok') in rows


def test_local_delete_rejects_active_conversation_then_hides_run(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def blocked(config, question):
        entered.set()
        assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = blocked
    with client_for(app) as client:
        cid = client.post('/api/conversations', json={'datasource_id': 'shop'}).json()['id']
        path = '/api/conversations/' + cid
        rid = client.post(path+'/messages', json={'question': 'Title'}).json()['id']
        try:
            assert entered.wait(5)
            response = client.delete(path)
            assert response.status_code == 400 and '运行' in response.json()['error']
        finally:
            release.set()
        wait_done(app)
        assert client.get('/api/user/run', params={'id': rid}).is_success
        assert client.delete(path).json() == {'deleted': [cid]}
        assert not client.get(path).is_success
        assert not client.get('/api/user/run', params={'id': rid}).is_success
        assert rid not in [entry['id'] for entry in client.get('/api/user/runs').json()['runs']]
        assert (app.output / rid / 'result.json').exists()


def test_shared_source_pilot_survives_backup_and_restore(tmp_path, monkeypatch):
    from sql_agent_lab.workspace.backup import create_backup, restore_backup, verify_backup
    from sql_agent_lab.workspace.metric_definition import PAID_TOTAL_TITLE

    base = build(tmp_path, monkeypatch)
    with closing(sqlite3.connect(base.databases['shop'])) as conn:
        conn.executescript(
            "CREATE TABLE orders(year INTEGER, status TEXT, amount INTEGER);"
            "INSERT INTO orders VALUES (2024,'paid',42),(2023,'paid',17),(2024,'cancelled',500);"
        )
    metric = dict(table='orders', year_column='year', status_column='status',
                  amount_column='amount', paid_value='paid')
    base.add_source_knowledge('shop', dict(title=PAID_TOTAL_TITLE, format='json', body=json.dumps(metric)))

    def no_model(*args, **kwargs):
        raise AssertionError('Pilot acceptance must not initialize a model')

    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.LocalOllamaModel', no_model)
    tokens = [uuid4().hex, uuid4().hex]
    config = tmp_path / 'accounts.json'
    config.write_text(json.dumps([
        {'id': str(uuid4()), 'name': name, 'sources': ['shop'],
         'token_sha256': sha256(token.encode()).hexdigest()}
        for name, token in zip(('Alice', 'Bob'), tokens)
    ]), encoding='utf-8')
    sources = tmp_path / 'sources.json'
    app = Observatory(None, base.output, workspace_db=base.workspace_db, sources=sources, accounts_config=config)

    def sign_in(client, token):
        assert client.post('/api/session', json={'token': token}).is_success
        assert [source['id'] for source in client.get('/api/catalog').json()['sources']] == ['shop']

    def ask(client, title, year):
        created = client.post('/api/conversations', json={'datasource_id': 'shop', 'title': title})
        assert created.is_success, created.text
        cid = created.json()['id']
        sent = client.post(f'/api/conversations/{cid}/messages',
                           json={'question': f'统计{year}年已付款订单总金额'})
        assert sent.is_success, sent.text
        wait_idle(app)
        return cid, sent.json()['id']

    with client_for(app) as alice, httpx.Client(base_url=alice.base_url, trust_env=False) as bob:
        sign_in(alice, tokens[0])
        sign_in(bob, tokens[1])
        remove_one, raw_run = ask(alice, 'Alice remove one', 2024)
        remove_two, _ = ask(alice, 'Alice remove two', 2023)
        keep, _ = ask(alice, 'Alice keep', 2024)
        other, bob_run = ask(bob, 'Bob private', 2023)

    old = tmp_path / 'before-deletion'
    create_backup(base.workspace_db, base.output, old, sources=sources)
    with client_for(app) as alice, httpx.Client(base_url=alice.base_url, trust_env=False) as bob:
        sign_in(alice, tokens[0])
        sign_in(bob, tokens[1])
        assert alice.get(f'/api/conversations/{keep}').json()['runs'][0]['result']['rows'] == [[42]]
        assert bob.get(f'/api/conversations/{other}').json()['runs'][0]['result']['rows'] == [[17]]
        assert alice.get(f'/api/conversations/{other}').status_code == 404
        assert bob.get(f'/api/conversations/{keep}').status_code == 404
        assert alice.get(f'/api/user/run?id={bob_run}').status_code == 404
        rejected = alice.post('/api/conversations/delete', json={'ids': [remove_one, other]})
        assert rejected.status_code == 404
        assert alice.get(f'/api/conversations/{remove_one}').is_success
        deleted = alice.post('/api/conversations/delete', json={'ids': [remove_one, remove_two]})
        assert deleted.json()['deleted'] == [remove_one, remove_two]
        assert alice.get(f'/api/conversations/{remove_one}').status_code == 404
        assert bob.get(f'/api/conversations/{other}').is_success

    fresh = tmp_path / 'after-deletion'
    create_backup(base.workspace_db, base.output, fresh, sources=sources)
    for key, backup, deletions in [
        ('restore-current', fresh, None),
        ('restore-older-with-deletions', old, base.workspace_db),
    ]:
        dest = tmp_path / key
        assert restore_backup(backup, dest, deletions_from=deletions) == len(verify_backup(backup))
        recovered = Observatory(None, dest / 'runs', sources=dest / 'sources.json',
                                workspace_db=dest / 'workspace.sqlite', accounts_config=config)
        with client_for(recovered) as alice, httpx.Client(base_url=alice.base_url, trust_env=False) as bob:
            sign_in(alice, tokens[0])
            sign_in(bob, tokens[1])
            assert [c['id'] for c in alice.get('/api/conversations').json()['conversations']] == [keep]
            assert [c['id'] for c in bob.get('/api/conversations').json()['conversations']] == [other]
            for cid in (remove_one, remove_two):
                assert alice.get(f'/api/conversations/{cid}').status_code == 404
            assert alice.get(f'/api/run?id={raw_run}').status_code == 404
            assert alice.get(f'/api/conversations/{other}').status_code == 404
            assert bob.get(f'/api/conversations/{keep}').status_code == 404
            assert alice.get(f'/api/conversations/{keep}').json()['runs'][0]['result']['rows'] == [[42]]
            assert bob.get(f'/api/conversations/{other}').json()['runs'][0]['result']['rows'] == [[17]]
