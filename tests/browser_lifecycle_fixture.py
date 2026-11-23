"""Manual browser acceptance server. Synthetic data; all outbound sockets denied.

Run with this repository's Python and --root pointing to a NEW directory outside
the checkout. It prints its random loopback URL. Touch root/release to finish a
waiting fake request, or root/shutdown to stop this fixture server. Reusing its
marked root models a service restart without editing any saved task.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.macsql.task_lifecycle import change_task, new_task, write_json_atomic
from sql_agent_lab.macsql.tools import SQLiteTools


def seed_answer_cases(app):
    """Saved UI examples from actual synthetic SQLite execution, with zero model calls."""
    tools = SQLiteTools(app.databases)
    cases = [
        ('scalar', '单值：统计记录总数', 'SELECT COUNT(*) AS 记录总数 FROM numbers'),
        ('zero', '零值：统计不存在的记录', 'SELECT COUNT(*) AS 记录总数 FROM numbers WHERE 1=0'),
        ('empty', '空结果：查询不存在的记录', 'SELECT n FROM numbers WHERE 1=0'),
        ('null', '空值：对空集合求最大值', 'SELECT MAX(n) AS 最大值 FROM numbers WHERE 1=0'),
        ('failure', '执行失败：查询不存在的字段', 'SELECT missing_column FROM numbers')]
    for name, question, sql in cases:
        directory = app.output / ('answer-' + name)
        if directory.exists():
            continue
        directory.mkdir()
        feedback = tools.execute_sql_readonly('first', sql)
        status = 'completed' if feedback['status'] == 'ok' else 'failed'
        write_json_atomic(directory / 'manifest.json', {
            'question': {'question_id': 'custom', 'db_id': 'first', 'question': '离线示例 · ' + question, 'evidence': ''},
            'model': {'name': '无模型 SQLite 示例'}, 'mode': 'offline_fixture', 'benchmark': False})
        write_json_atomic(directory / 'result.json', {'status': status, 'sql': sql,
            'feedback': feedback, 'score': None, 'cloud_api_calls': 0})
        write_json_atomic(directory / 'task.json', change_task(new_task(), state=status))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--standalone', action='store_true', help='Two SQLite sources without any BIRD files')
    parser.add_argument('--showcase', action='store_true', help='Use synthetic shop data and fixed SQL (requires --standalone)')
    parser.add_argument('--workspace', action='store_true', help='Enable the workspace with synthetic sources')
    parser.add_argument('--empty-workspace', action='store_true',
                        help='Start with no sources so the first upload can be tested')
    parser.add_argument('--answer-cases', action='store_true', help='Save scalar/zero/empty/null/error examples without models')
    args = parser.parse_args()
    if args.empty_workspace:
        if args.showcase or args.answer_cases:
            parser.error('--empty-workspace cannot seed examples')
        args.standalone = args.workspace = True
    if args.showcase and not args.standalone:
        parser.error('--showcase requires --standalone')
    if args.answer_cases and not args.standalone:
        parser.error('--answer-cases requires --standalone')
    if args.workspace and not args.standalone:
        parser.error('--workspace requires --standalone')
    root = args.root.resolve()
    checkout = Path(__file__).resolve().parents[1]
    if root == checkout or checkout in root.parents:
        parser.error('Use a new fixture directory outside the checkout')
    if root.exists() and not (root / 'offline-fixture.json').is_file():
        parser.error('Refusing to reuse an unmarked directory')
    root.mkdir(parents=True, exist_ok=True)
    metrics = {'fixture': True, 'real_model_calls': 0, 'blocked_outbound_connections': 0}

    def deny_connections(event, values):
        if event == 'socket.connect':
            metrics['blocked_outbound_connections'] += 1
            raise RuntimeError('Offline browser fixture blocks all outbound connections')

    sys.addaudithook(deny_connections)
    data = root / 'data'
    if not data.exists() and not args.standalone:
        data.mkdir()
        db = data / 'demo.sqlite'
        with sqlite3.connect(db) as conn:
            conn.executescript('CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES (42);')
        questions = data / 'mini_dev_sqlite.jsonl'
        cases = [('2', '离线验收：正常完成'), ('3', '离线验收：任务完成但答案错误'),
                 ('4', '离线验收：模拟执行异常'), ('5', '离线验收：等待中的任务，用于停止或重启')]
        questions.write_text(''.join(json.dumps({'question_id': qid, 'db_id': 'demo',
            'question': title, 'SQL': 'SELECT n FROM numbers'}, ensure_ascii=False) + '\n'
            for qid, title in cases), encoding='utf8')
        (data / 'source-lock.json').write_text(json.dumps({'annotations_sha256': sha256(questions),
            'databases': {'demo': {'path': 'demo.sqlite', 'sha256': sha256(db)}}}), encoding='utf8')
    if args.standalone:
        sources = root / 'sources.json'
        if not sources.exists():
            entries = []
            seeds = [] if args.empty_workspace else [('first', 42), ('second', 99)]
            for source_id, value in seeds:
                db = root / (source_id + '.sqlite')
                conn = sqlite3.connect(db)
                try:
                    conn.executescript(f'CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES ({value});')
                    if args.showcase:
                        conn.execute('CREATE TABLE sales(category TEXT, product TEXT, quantity INTEGER, amount REAL)')
                        conn.executemany('INSERT INTO sales VALUES (?,?,?,?)', [
                            ('生活家居', '收纳盒', 320, 12800), ('生活家居', '香氛', 186, 10974),
                            ('数码配件', '充电线', 268, 13266), ('数码配件', '键盘', 124, 18500),
                            ('文具办公', '笔记本', 218, 6540), ('文具办公', '中性笔', 137, 2740),
                            ('运动户外', '保温杯', 176, 14080), ('运动户外', '运动毛巾', 81, 2835),
                            ('食品饮品', '挂耳咖啡', 145, 8700), ('食品饮品', '燕麦棒', 68, 2040)])
                        conn.commit()
                finally:
                    conn.close()
                name = ('演示商店 · 合成数据' if source_id == 'first' else '备用演示库 · 合成数据') if args.showcase else f'离线数据 {value}'
                entries.append({'id': source_id, 'name': name, 'path': db.name})
            sources.write_text(json.dumps({'sources': entries}), encoding='utf8')
        app = live_ui.Observatory(None, root / 'runs', sources=sources,
                                  workspace_db=root / 'workspace.sqlite' if args.workspace else None)
    else:
        app = live_ui.Observatory(data, root / 'runs')
    if args.answer_cases:
        seed_answer_cases(app)
    analysis = root / 'analysis'
    analysis.mkdir(exist_ok=True)
    report = dict(rows=[], stages={}, definitions={}, versions=[], priorities=[], inventory=[],
                  excluded=[], warnings=[], generated_at='2026-01-01T00:00:00Z',
                  recommendation='', hpc='', scope_note='Synthetic fixture. No saved evaluation runs.')
    (analysis / 'analysis.json').write_text(json.dumps(report), encoding='utf8')
    app.analysis_dir = analysis

    class FakeAgent:
        def __init__(self, models, *, config):
            self.config = config

        def run(self, question):
            emit = self.config.trace
            emit({'event': 'call_started', 'role': 'decomposer', 'call_id': 'offline',
                  'messages': [], 'simulated': True})
            if question.question_id == '5' or (args.workspace and question.question == 'Wait for release'):
                deadline = time.monotonic() + 300
                while not (root / 'release').exists():
                    if time.monotonic() > deadline:
                        raise TimeoutError('Offline fixture wait expired')
                    if args.workspace and self.config.tools.stop.wait(.1):
                        raise live_ui.Stopped('Stopped during the offline fixture wait')
                    if not args.workspace:
                        time.sleep(.1)
            if question.question_id == '4' or (args.workspace and question.question == 'Simulate a failed query'):
                raise RuntimeError('离线注入异常：用于验证失败状态')
            sql = 'SELECT n+1 AS n FROM numbers' if question.question_id == '3' else 'SELECT n FROM numbers'
            if args.showcase:
                # A fixed fixture response, never natural-language inference.
                sql = ('SELECT category AS 商品品类, COUNT(*) AS 商品种类, '
                       'SUM(quantity) AS 销售数量, ROUND(SUM(amount), 2) AS 销售额 '
                       'FROM sales GROUP BY category ORDER BY 销售数量 DESC')
            emit({'event': 'call_completed', 'role': 'decomposer', 'call_id': 'offline',
                  'response': sql, 'input_tokens': 0, 'output_tokens': 0, 'simulated': True})
            emit({'event': 'workflow_completed', 'sql': sql, 'semantic_status': 'disabled',
                  'output_type_check': {'status': 'unverified'}})
            return sql

    class FixtureHandler(live_ui.make_handler(app)):
        def send(self, value, status=200, content_type='application/json'):
            if content_type == 'text/html':
                banner = '<div style="padding:12px;background:#fff4dc">离线浏览器验收：模拟 Agent；所有出站连接已禁用；仅使用临时数据。</div>'
                value = value.replace(b'<header>', (banner + '<header>').encode('utf8'), 1)
            return super().send(value, status, content_type)

    with patch.object(live_ui, 'LocalOllamaModel', lambda *a, **k: SimpleNamespace(metadata={'fixture': True})), \
         patch.object(live_ui, 'ReferenceMacSQLAgent', FakeAgent):
        server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        server.timeout = .2
        address = f'http://127.0.0.1:{server.server_port}'
        (root / 'offline-fixture.json').write_text(json.dumps({**metrics, 'url': address}), encoding='utf8')
        print(json.dumps({'url': address, 'root': str(root), 'outbound_network': 'denied'}), flush=True)
        try:
            while not (root / 'shutdown').exists():
                server.handle_request()
        finally:
            server.server_close()
            (root / 'fixture-metrics.json').write_text(json.dumps(metrics), encoding='utf8')


if __name__ == '__main__':
    main()
