"""Offline contract tests: no API keys or real model requests."""
import asyncio
import json

import httpx
import pytest

from .deepseek_api import APIRunError, DeepSeekModel, Ledger, MODEL, read_events


def body(reason='stop', usage=True):
    result = dict(id='fake-id', model='deepseek-v4.1-flash',
                  choices=[dict(message={'content': '{"ok":true}'}, finish_reason=reason)])
    if usage:
        result['usage'] = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                               completion_tokens_details={'reasoning_tokens': 5}, prompt_cache_hit_tokens=60)
    return result


def call(model, role='decomposer', text='question'):
    return model(role=role, model=MODEL, messages=[{'role': 'user', 'content': text}], max_output_tokens=1000)


def test_complex_output_budget_is_reserved_without_lifting_total_cap(tmp_path):
    payloads=[]
    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200,json=body())
    ledger=Ledger(tmp_path/'large-output.jsonl',question_cap=4000,batch_cap=4000,max_calls=3)
    model=DeepSeekModel('test-secret',ledger,'synthetic',transport=httpx.MockTransport(respond))
    model(role='decomposer',model=MODEL,messages=[{'role':'user','content':'question'}],max_output_tokens=2400)
    assert payloads[0]['max_tokens']==2400
    assert read_events(ledger.path)[0]['reservation_tokens']>=2400
    with pytest.raises(APIRunError):
        model(role='refiner',model=MODEL,messages=[{'role':'user','content':'x'*1500}],max_output_tokens=2400)
    assert len(payloads)==1


@pytest.mark.parametrize('marker', ['SEMANTIC_REVIEW_V3', 'SEMANTIC_REVIEW_V4', 'SEMANTIC_REVIEW_V5', 'SEMANTIC_REVIEW_V6', 'SEMANTIC_REVIEW_V7', 'SEMANTIC_REVIEW_V8', 'OUTPUT_REQUIREMENTS_V1', 'OUTPUT_REQUIREMENTS_V2', 'OUTPUT_REQUIREMENTS_V3'])
def test_json_contract_usage_and_secret_not_logged(tmp_path, marker):
    payloads = []
    def handler(request):
        assert str(request.url) == 'https://api.deepseek.com/chat/completions'
        assert request.headers['authorization'] == 'Bearer test-secret'
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=body())
    ledger = Ledger(tmp_path/'calls.jsonl')
    model = DeepSeekModel('test-secret', ledger, '118', transport=httpx.MockTransport(handler))
    for role, text in [('decomposer','q'), ('selector','q'), ('refiner',marker+' q')]:
        reply = call(model, role, text)
        assert (reply.input_tokens, reply.output_tokens) == (100, 20)
    assert 'response_format' not in payloads[0]
    assert payloads[1]['response_format'] == payloads[2]['response_format'] == {'type':'json_object'}
    assert all(p['thinking'] == {'type': 'disabled'} and p['stream'] is False for p in payloads)
    assert ledger.totals() == (360, 3, False)  # Do not count reasoning tokens twice.
    assert 'test-secret' not in ledger.path.read_text()


@pytest.mark.parametrize('reason', ['length', 'content_filter'])
def test_rejected_response_still_counts_actual_usage(tmp_path, reason):
    ledger = Ledger(tmp_path/'calls.jsonl')
    model = DeepSeekModel('dummy', ledger, 118, transport=httpx.MockTransport(lambda r: httpx.Response(200,json=body(reason))))
    with pytest.raises(APIRunError) as caught:
        call(model)
    assert caught.value.raw_usage['prompt_eval_count'] == 100
    assert ledger.totals() == (120, 1, False)


@pytest.mark.parametrize('scenario', ['missing_usage', 'http_error', 'timeout'])
def test_unknown_usage_blocks_any_next_request_without_retry(tmp_path, scenario):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        if scenario == 'timeout':
            await asyncio.sleep(.1)
        return httpx.Response(401 if scenario == 'http_error' else 200, json=body(usage=False))
    ledger = Ledger(tmp_path/'calls.jsonl')
    model = DeepSeekModel('dummy', ledger, 118, transport=httpx.MockTransport(handler), request_timeout=.01)
    with pytest.raises(APIRunError):
        call(model)
    assert ledger.totals()[2]
    with pytest.raises(APIRunError, match='usage is unknown'):
        call(model)
    assert count == 1


@pytest.mark.parametrize('limits', [dict(batch_cap=100), dict(question_cap=100), dict(max_calls=0)])
def test_budget_blocks_before_network(tmp_path, limits):
    def unexpected(request):
        pytest.fail('Budget exhaustion must not send an HTTP request')
    ledger = Ledger(tmp_path/'calls.jsonl', **limits)
    model = DeepSeekModel('dummy', ledger, 118, transport=httpx.MockTransport(unexpected))
    with pytest.raises(APIRunError, match='budget'):
        call(model)
    assert not ledger.path.exists()


def test_pending_request_survives_process_restart(tmp_path):
    ledger = Ledger(tmp_path/'calls.jsonl')
    ledger.reserve('118','decomposer',[{'content':'q'}],1000)
    with pytest.raises(APIRunError, match='usage is unknown'):
        Ledger(ledger.path).reserve('165','decomposer',[{'content':'q'}],1000)


def test_call_limit_counts_failed_paid_calls(tmp_path):
    ledger = Ledger(tmp_path/'calls.jsonl', max_calls=1)
    model = DeepSeekModel('dummy', ledger,118,transport=httpx.MockTransport(lambda r:httpx.Response(200,json=body('length'))))
    with pytest.raises(APIRunError):
        call(model)
    with pytest.raises(APIRunError, match='budget'):
        call(model)
    assert len(read_events(ledger.path)) == 2
