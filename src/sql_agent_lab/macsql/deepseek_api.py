"""Local experiment adapter for the official DeepSeek endpoint; no automatic retries."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from .reference_core import ModelReply

MODEL = 'deepseek-flash'
ENDPOINT = 'https://api.deepseek.com/chat/completions'


class APIRunError(RuntimeError):
    def __init__(self, code, message, *, usage=None, response='', done_reason=None):
        super().__init__(message)
        self.code, self.raw_usage = code, usage or {}
        self.response, self.done_reason = response, done_reason


def read_events(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()] if path.exists() else []


class Ledger:
    """Single sequential runner. Reserve conservatively before sending; never refund unknown usage."""
    def __init__(self, path, *, batch_cap=80000, question_cap=20000, max_calls=4):
        self.path = Path(path)
        self.batch_cap, self.question_cap, self.max_calls = batch_cap, question_cap, max_calls

    def append(self, event):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf8') as f:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
            f.flush()

    def totals(self, qid=None):
        events = read_events(self.path)
        starts = {e['id']: e for e in events if e['event'] == 'request_started'}
        ends = {e['id']: e for e in events if e['event'] == 'request_finished'}
        chosen = [s for s in starts.values() if qid is None or s['question_id'] == str(qid)]
        tokens = sum(ends[s['id']]['total_tokens'] if s['id'] in ends and ends[s['id']].get('usage_known')
                     else s['reservation_tokens'] for s in chosen)
        unknown = any(s['id'] not in ends or not ends[s['id']].get('usage_known') for s in starts.values())
        return tokens, len(chosen), unknown

    def reserve(self, qid, role, messages, output_limit):
        # UTF-8 bytes plus generous chat framing are an admission bound, not billed usage.
        reserved = 512 + sum(len(m['content'].encode('utf8')) + 32 for m in messages) + output_limit
        batch, _, unknown = self.totals()
        question, calls, _ = self.totals(qid)
        if unknown:
            raise APIRunError('unknown_usage', 'Previous request usage is unknown; batch is stopped')
        if calls >= self.max_calls or batch + reserved > self.batch_cap or question + reserved > self.question_cap:
            raise APIRunError('budget_exhausted', 'Next request would exceed the call/token reservation budget')
        record = dict(event='request_started', id=uuid4().hex, question_id=str(qid), role=role,
                      model=MODEL, reservation_tokens=reserved, output_limit=output_limit,
                      at=datetime.now(timezone.utc).isoformat())
        self.append(record)
        return record


class DeepSeekModel:
    def __init__(self, key, ledger, qid, *, transport=None, request_timeout=120):
        if not key or key != key.strip() or not key.isascii():
            raise ValueError('Missing or malformed DEEPSEEK_API_KEY (value is never logged)')
        self._key, self.ledger, self.qid = key, ledger, str(qid)
        self.transport, self.request_timeout = transport, request_timeout

    async def _request(self, payload):
        async with httpx.AsyncClient(transport=self.transport, trust_env=False,
                                    follow_redirects=False, timeout=self.request_timeout) as client:
            return await asyncio.wait_for(client.post(ENDPOINT, json=payload,
                headers={'Authorization': 'Bearer ' + self._key}), timeout=self.request_timeout)

    def __call__(self, *, role, model, messages, max_output_tokens):
        if model != MODEL:
            raise ValueError('Only the selected DeepSeek Flash model is permitted')
        if not 1 <= max_output_tokens <= 3200:
            raise ValueError('Pilot output limit must be between 1 and 3200')
        is_json = role == 'selector' or any(m['content'].startswith(('SEMANTIC_REVIEW_V3', 'SEMANTIC_REVIEW_V4', 'SEMANTIC_REVIEW_V5', 'SEMANTIC_REVIEW_V6', 'SEMANTIC_REVIEW_V7', 'SEMANTIC_REVIEW_V8', 'OUTPUT_REQUIREMENTS_V1', 'OUTPUT_REQUIREMENTS_V2', 'OUTPUT_REQUIREMENTS_V3')) for m in messages)
        payload = dict(model=MODEL, messages=messages, max_tokens=max_output_tokens,
                       temperature=0, stream=False, thinking={'type': 'disabled'})
        if is_json:
            payload['response_format'] = {'type': 'json_object'}
        started = self.ledger.reserve(self.qid, role, messages, max_output_tokens)
        clock = time.monotonic()
        print(f'Question {self.qid}: calling {role}', flush=True)
        record = dict(event='request_finished', id=started['id'], question_id=self.qid, role=role,
                      usage_known=False, total_tokens=None)
        body, usage, content, reason = {}, {}, '', None
        try:
            response = asyncio.run(self._request(payload))
            record['http_status'] = response.status_code
            if response.status_code != 200:
                raise APIRunError('http_error', f'DeepSeek HTTP {response.status_code}; no automatic retry')
            body = response.json()
            usage = body.get('usage', {})
            p, c, t = (usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            if not all(type(n) is int and n >= 0 for n in (p, c, t)) or t != p + c:
                raise APIRunError('missing_usage', 'Missing or inconsistent provider token usage')
            # Completion tokens already include reasoning tokens; do not add them again.
            usage = {**usage, 'prompt_eval_count': p, 'eval_count': c,
                     'response_format_contract': 'json' if is_json else 'text'}
            record.update(usage_known=True, total_tokens=t, input_tokens=p, output_tokens=c,
                          usage=usage, response_id=body.get('id'), returned_model=body.get('model'),
                          estimated_cost_usd_peak_no_cache=(p * .30 + c * 1.20) / 1e6)
            choice = body['choices'][0]
            content, reason = choice['message'].get('content') or '', choice.get('finish_reason')
            usage.update(finish_reason=reason, ledger_request_id=started['id'], returned_model=body.get('model'))
            record.update(response=content, finish_reason=reason)
            if t > started['reservation_tokens']:
                raise APIRunError('reservation_underestimated', 'Provider usage exceeded conservative reservation')
            if reason != 'stop' or not content.strip():
                raise APIRunError('output_truncated' if reason == 'length' else 'invalid_response',
                                  'Model response is incomplete or empty')
            record['status'] = 'completed'
            return ModelReply(content, p, c, response_id=body.get('id', ''), raw_usage=usage)
        except Exception as error:
            code = error.code if isinstance(error, APIRunError) else type(error).__name__
            # Never persist request headers, exception reprs or the secret.
            record.update(status='failed', error_code=code)
            raise APIRunError(code, f'DeepSeek request failed: {code}', usage=usage,
                              response=content, done_reason=reason) from None
        finally:
            record['elapsed_seconds'] = time.monotonic() - clock
            self.ledger.append(record)

