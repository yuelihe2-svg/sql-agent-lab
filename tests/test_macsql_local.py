"""Local Ollama adapter contracts without running a model."""

import json

import httpx
import pytest

pytest.importorskip("langgraph")

from sql_agent_lab.macsql.run_local import LocalOllamaModel


def test_reference_roles_and_usage():
    requests = []

    def respond(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "local-test",
                                                        "details": {"format": "gguf"}}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "stop",
                                         "message": {"content": "```sql\\nSELECT 1\\n```"},
                                         "prompt_eval_count": 31, "eval_count": 7})

    with httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(respond)) as client:
        model = LocalOllamaModel(client, "local-test", num_threads=8, cpu_only=True)
        for role in ("selector", "decomposer", "refiner"):
            reply = model(role=role, model="local-test", messages=[], max_output_tokens=1000)
            assert (reply.input_tokens, reply.output_tokens) == (31, 7)
            assert not reply.simulated
        model(role='refiner',model='local-test',messages=[{'role':'user','content':'SEMANTIC_REVIEW_V2 test'}],max_output_tokens=1000)
        model(role='refiner',model='local-test',messages=[{'role':'user','content':'SEMANTIC_REVIEW_V3 test'}],max_output_tokens=1000)
        for marker in ('SEMANTIC_REVIEW_V8','OUTPUT_REQUIREMENTS_V3'):
            model(role='refiner',model='local-test',messages=[{'role':'user','content':marker+' test'}],max_output_tokens=1000)
        with pytest.raises(ValueError, match="validated local model"):
            model(role="decomposer", model="another", messages=[], max_output_tokens=1000)

    assert requests[0]["format"] == "json"
    assert all("format" not in request for request in requests[1:3])
    assert requests[3]['format']=='json'
    assert requests[4]['format']=='json'
    assert requests[5]['format']==requests[6]['format']=='json'
    for request in requests:
        assert request["options"] == {"num_ctx": 8192, "num_predict": 1000,
                                      "temperature": 0, "seed": 123, "num_thread": 8, "num_gpu": 0}
        assert request["think"] is False and request["stream"] is False


def test_local_adapter_rejects_external_server_before_request():
    def unexpected(request):
        raise AssertionError("No request should be sent")

    with httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError, match="loopback"):
            LocalOllamaModel(client, "local-test")


def test_local_adapter_rejects_cloud_model():
    with httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"models": [{"name": "test:cloud",
                                                             "details": {"format": "gguf"}}]})
    )) as client:
        with pytest.raises(ValueError, match="local GGUF"):
            LocalOllamaModel(client, "test:cloud")


@pytest.mark.parametrize('with_usage', [True, False])
def test_truncated_reply_preserves_failure_usage_in_trace(with_usage):
    from sql_agent_lab.macsql.run_local import ModelResponseError
    from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
    from sql_agent_lab.macsql.reference_core import WorkflowConfig, ROLES
    from sql_agent_lab.macsql.dataset import Question
    body = {'done': True, 'done_reason': 'length', 'message': {'content': '{"action":'}}
    if with_usage:
        body.update(prompt_eval_count=123, eval_count=1600)
    def respond(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': 'local-test', 'details': {'format': 'gguf'}}]})
        return httpx.Response(200, json={} if request.url.path == '/api/show' else body)
    events = []
    with httpx.Client(base_url='http://127.0.0.1:11434', transport=httpx.MockTransport(respond)) as client:
        model = LocalOllamaModel(client, 'local-test')
        agent = ReferenceMacSQLAgent({r:'local-test' for r in ROLES}, config=WorkflowConfig(None, model, trace=events.append))
        with pytest.raises(ModelResponseError) as caught:
            agent._ask_reference({'run_id':'test', 'question':Question('q','db','A question')},
                                 'refiner', [], {'max_output_tokens':1600, 'purpose':'semantic_review'})
    assert caught.value.code == 'output_truncated'
    assert [e['event'] for e in events] == ['context_prepared', 'call_started', 'call_failed']
    failed = events[-1]
    assert failed['response'] == '{"action":' and failed['done_reason'] == 'length'
    assert failed['purpose'] == 'semantic_review'
    assert failed['usage_complete'] is with_usage
    assert failed['input_tokens'] == (123 if with_usage else None)
    assert failed['output_tokens'] == (1600 if with_usage else None)
