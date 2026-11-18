"""Legacy run directories project to the same task state without being rewritten."""
import inspect
from pathlib import Path

import pytest

from sql_agent_lab.macsql.task_lifecycle import change_task, new_task, task_view, write_json_atomic
from sql_agent_lab.workspace import projection
from sql_agent_lab.workspace.projection import project_legacy_run


def files(path):
    return {item.relative_to(path).as_posix(): item.read_bytes()
            for item in path.rglob('*') if item.is_file()}


def write_run(root, name, *, manifest, task=None, result=None, evidence=None):
    path = root / name
    path.mkdir()
    write_json_atomic(path / 'manifest.json', manifest)
    if task is not None:
        write_json_atomic(path / 'task.json', task)
    if result is not None:
        write_json_atomic(path / 'result.json', result)
    if evidence is not None:
        write_json_atomic(path / 'evaluation-evidence.json', evidence)
        (path / 'trace.jsonl').write_text('{"event":"tool_started","rows":["sample-row"]}\n',
                                          encoding='utf8')
    return path


def manifest(task, question='How many rows?', *, semantic_review=False, question_id='custom'):
    return {'question': {'question_id': question_id, 'db_id': 'demo', 'question': question,
                         'evidence': ''},
            'semantic_review': semantic_review,
            'created_at': task['started_at'] if task else '2026-11-18T20:00:00+00:00'}


def assert_matches(run, view):
    assert run.state == view['state']
    assert run.stage == (view.get('stage') or None)
    assert run.started_at == view.get('started_at')
    assert run.updated_at == view.get('updated_at')
    assert run.ended_at == view.get('ended_at')
    assert run.stop_requested_at == view.get('stop_requested_at')
    assert run.error == (view.get('error') or None)


def test_projection_matches_saved_task_state(tmp_path):
    task = change_task(new_task(), state='completed')
    path = write_run(tmp_path, 'scored', manifest=manifest(task, semantic_review=True, question_id='1'),
                     task=task, result={'status': 'completed', 'score': 1})
    before = files(path)
    run, message = project_legacy_run(path)
    assert files(path) == before
    assert_matches(run, task_view(path, {'status': 'completed', 'score': 1}))
    assert run.score == 1
    assert run.result_status == 'completed'
    assert run.semantic_review is True
    assert run.datasource_id == 'demo'
    assert message.body == 'How many rows?'
    assert project_legacy_run(path) == (run, message)


def test_custom_question_without_reference_has_empty_score(tmp_path):
    task = change_task(new_task(), state='completed')
    result = {'status': 'completed', 'score': None, 'sql': 'SELECT gold_sentinel FROM answer',
              'feedback': {'rows': [['business-row-sentinel']]}}
    path = write_run(tmp_path, 'custom-run', manifest=manifest(task), task=task, result=result,
                     evidence={'samples': [['evidence-row-sentinel']], 'reference_sql': 'SELECT gold_sentinel'})
    before = files(path)
    run, message = project_legacy_run(path)
    assert files(path) == before
    assert_matches(run, task_view(path, result))
    assert run.score is None
    assert run.result_status == 'completed'
    assert run.conversation_id is None
    assert run.id == 'custom-run'
    assert message.conversation_id is None
    blob = run.to_json() + message.to_json()
    assert 'gold_sentinel' not in blob
    assert 'business-row-sentinel' not in blob
    assert 'evidence-row-sentinel' not in blob
    assert str(path.resolve()) not in blob


def test_projection_does_not_read_evaluation_evidence(tmp_path, monkeypatch):
    task = change_task(new_task(), state='completed')
    path = write_run(tmp_path, 'hidden', manifest=manifest(task), task=task,
                     result={'status': 'failed', 'score': 0},
                     evidence={'samples': [['evidence-row-sentinel']]})
    original_read = Path.read_text
    original_open = Path.open

    def guard_read(self, *args, **kwargs):
        assert self.name not in {'evaluation-evidence.json', 'trace.jsonl'}
        return original_read(self, *args, **kwargs)

    def guard_open(self, *args, **kwargs):
        assert self.name not in {'evaluation-evidence.json', 'trace.jsonl'}
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guard_read)
    monkeypatch.setattr(Path, 'open', guard_open)
    run, _message = project_legacy_run(path)
    assert run.score == 0
    assert 'evaluation-evidence' not in inspect.getsource(projection)


def test_interrupted_and_recovered_states_match_task_view(tmp_path):
    stopping = change_task(new_task(), state='stopping')
    abandoned = write_run(tmp_path, 'abandoned', manifest=manifest(stopping), task=stopping)
    before = files(abandoned)
    run, _message = project_legacy_run(abandoned, managed=True)
    view = task_view(abandoned, None, managed=True)
    assert run.state == 'interrupted'
    assert run.score is None
    assert run.result_status is None
    assert_matches(run, view)
    assert files(abandoned) == before

    running = new_task()
    reviewed = write_run(tmp_path, 'reviewed', manifest=manifest(running), task=running,
                         result={'status': 'needs_review', 'score': 0,
                                 'sql': 'SELECT gold_sentinel FROM answer'})
    run, message = project_legacy_run(reviewed)
    view = task_view(reviewed, {'status': 'needs_review', 'score': 0})
    assert view['state'] == 'completed'
    assert view['source'] == 'result_recovery'
    assert_matches(run, view)
    assert run.result_status == 'needs_review'
    assert run.score == 0
    assert 'gold_sentinel' not in run.to_json() + message.to_json()

    stopped_task = change_task(change_task(new_task(), state='stopping'), state='stopped',
                               error='Stopped before the next model call')
    stopped = write_run(tmp_path, 'stopped-run', manifest=manifest(stopped_task), task=stopped_task,
                        result={'status': 'stopped', 'score': None})
    run, _message = project_legacy_run(stopped)
    assert_matches(run, task_view(stopped, {'status': 'stopped', 'score': None}))
    assert run.state == 'stopped'
    assert run.score is None

    legacy = write_run(tmp_path, 'legacy', manifest=manifest(None),
                       result={'status': 'completed', 'score': None})
    assert not (legacy / 'task.json').exists()
    run, _message = project_legacy_run(legacy)
    assert_matches(run, task_view(legacy, {'status': 'completed', 'score': None}))
    assert run.score is None
    assert not (legacy / 'task.json').exists()
