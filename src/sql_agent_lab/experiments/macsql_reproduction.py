"""Isolated BIRD reproduction of the pinned MAC-SQL control flow.

Production ReferenceMacSQLAgent is intentionally not subclassed: its context
compaction, repair acceptance and type gates are different research conditions.
The CLI only replays supplied replies and has no paid-run command.
"""
import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph

from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.deepseek_api import DeepSeekModel
from sql_agent_lab.macsql.reference_core import JsonlTrace, ModelReply
from sql_agent_lab.macsql.tools import EvaluationLimitExceeded, SQLiteTools, ToolLimits
from .macsql_schema import BirdSchema, bounded_rows, prepare_schema
from .macsql_upstream import REVISION, load_prompts


def parse_selection(text):
    start = text.find('```json')
    end = text.find('```', start + 7)
    if start == -1 or end == -1:
        return {}
    try:
        result = json.loads(text[start + 7:end])
        if isinstance(result, dict) and all(
            isinstance(v, list) or isinstance(v, str) and v in ('keep_all', 'drop_all')
            for v in result.values()
        ):
            return result
    except ValueError:
        pass
    return {}


def parse_final_sql(text):
    matches = re.findall(r'```sql(.*?)```', text, flags=re.DOTALL)
    return matches[-1].strip() if matches else 'error: No SQL found in the input string'


def refinement_reason(execution):
    if 'data' not in execution:
        return execution['sqlite_error']
    if not execution['data']:
        return 'no data selected'
    if any(value is None for row in execution['data'] for value in row):
        return 'exist None value, you can add `NOT NULL` in SQL'
    return ''


class ReproductionDeepSeekModel(DeepSeekModel):
    """Opt-in transport profile; construction itself makes no request.

    Keep the existing budget ledger and error handling, use upstream temperature
    0.1 and text responses (Selector includes reasoning plus a JSON code block).
    Ledger roles have a macsql_ prefix so they cannot be confused with app runs.
    """
    def __call__(self, *, role, **kwargs):
        return super().__call__(role='macsql_' + role, **kwargs)

    async def _request(self, payload):
        return await super()._request({**payload, 'temperature': 0.1})


class State(TypedDict, total=False):
    question: Question
    run_id: str
    schema_text: str
    foreign_keys: str
    selected_columns: dict
    selector_invoked: bool
    sql: str
    decomposition: str
    rounds: int
    repairs: int
    feedback: dict
    checked_sql: str | None
    final_sql_checked: bool
    stop_reason: str


@dataclass(frozen=True)
class ReproductionResult:
    sql: str
    selector_invoked: bool
    repairs: int
    rounds: int
    final_sql_checked: bool
    checked_sql: str | None
    last_execution: dict
    stop_reason: str
    upstream_revision: str = REVISION


class OriginalMacSQLAgent:
    def __init__(self, *, tools, model, model_name, schema: BirdSchema, upstream_dir,
                 trace=None, max_output_tokens=3200, max_prompt_bytes=100_000):
        if not 1 <= max_output_tokens <= 3200 or max_prompt_bytes <= 0:
            raise ValueError('Invalid explicit request limits')
        self.templates = load_prompts(upstream_dir)
        self.tools, self.model, self.model_name = tools, model, model_name
        self.schema, self.trace = schema, trace
        self.max_output_tokens, self.max_prompt_bytes = max_output_tokens, max_prompt_bytes
        graph = StateGraph(State)
        graph.add_node('selector', self._select)
        graph.add_node('decomposer', self._decompose)
        # One upstream round includes execution and, when needed, one repair.
        graph.add_node('refiner_round', self._refiner_round)
        graph.set_entry_point('selector')
        graph.add_edge('selector', 'decomposer')
        graph.add_edge('decomposer', 'refiner_round')
        graph.add_conditional_edges('refiner_round',
            lambda state: 'end' if state.get('stop_reason') else 'again',
            {'end': END, 'again': 'refiner_round'})
        self.graph = graph.compile()

    def _event(self, state, event, **fields):
        if self.trace:
            self.trace(dict(event=event, run_id=state['run_id'],
                            question_id=state['question'].question_id,
                            profile='macsql-official-bird') | {'upstream_revision': REVISION, **fields})

    def _ask(self, state, role, **fields):
        q = state['question']
        template = {'selector': 'selector_template', 'decomposer': 'decompose_template_bird',
                    'refiner': 'refiner_template'}[role]
        prompt = self.templates[template].format(
            db_id=q.db_id, query=q.question, evidence=q.evidence, **fields)
        if len(prompt.encode('utf8')) > self.max_prompt_bytes:
            # Never silently compact a supposedly original prompt.
            raise ValueError('Original prompt exceeds explicit byte limit; no request sent')
        messages = [{'role': 'user', 'content': prompt}]
        self._event(state, 'model_request', role=role, messages=messages,
                    max_output_tokens=self.max_output_tokens, model=self.model_name)
        reply = self.model(role=role, model=self.model_name, messages=messages,
                           max_output_tokens=self.max_output_tokens)
        self._event(state, 'model_response', role=role, text=reply.text,
                    input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                    response_id=reply.response_id, simulated=reply.simulated)
        return reply.text.strip()

    def _select(self, state):
        description, foreign_keys, columns = self.schema.render()
        decisions = {}
        if self.schema.needs_selector:
            reply = self._ask(state, 'selector', desc_str=description, fk_str=foreign_keys)
            decisions = parse_selection(reply)
            description, foreign_keys, columns = self.schema.render(decisions)
        self._event(state, 'schema_selected', selector_invoked=self.schema.needs_selector,
                    decisions=decisions, selected_columns=columns)
        return dict(schema_text=description, foreign_keys=foreign_keys,
                    selected_columns=columns, selector_invoked=self.schema.needs_selector)

    def _decompose(self, state):
        reply = self._ask(state, 'decomposer', desc_str=state['schema_text'], fk_str=state['foreign_keys'])
        return dict(sql=parse_final_sql(reply), decomposition=reply)

    def _execute(self, db_id, sql):
        try:
            with self.tools.connection(db_id, query=True) as conn:
                conn.text_factory = lambda b: b.decode(errors='ignore')
                cursor = conn.execute(sql)
                if cursor.description is None:
                    raise ValueError('Query must return rows')
                rows = bounded_rows(cursor, self.tools.limits)
            # All columns, exactly first five rows: UI truncation must not hide NULL.
            return dict(sql=sql, data=rows[:5], sqlite_error='', exception_class='')
        except EvaluationLimitExceeded:
            return dict(sql=sql, sqlite_error='Resource limit exceeded',
                        exception_class='EvaluationLimitExceeded', stop_reason='resource_limit')
        except (sqlite3.Error, ValueError) as error:
            result = dict(sql=sql, sqlite_error=str(error), exception_class=str(type(error)))
            if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                result['stop_reason'] = 'execution_timeout'
            return result

    def _refiner_round(self, state):
        sql = state['sql']
        rounds = state['rounds'] + 1
        # Literal case-sensitive upstream guard, including its false positives.
        if 'error' in sql:
            self._event(state, 'upstream_error_guard', sql=sql)
            return dict(rounds=rounds, final_sql_checked=False, stop_reason='upstream_error_guard')
        execution = self._execute(state['question'].db_id, sql)
        self._event(state, 'execution', feedback=execution, round=rounds)
        update = dict(rounds=rounds, feedback=execution, checked_sql=sql, final_sql_checked=True)
        if execution.get('stop_reason'):
            return {**update, 'stop_reason': execution['stop_reason']}
        reason = refinement_reason(execution)
        if not reason:
            return {**update, 'stop_reason': 'execution_accepted'}
        # Preserve upstream add_prefix behavior, even for WITH queries.
        prompt_sql = sql if sql.startswith(('SELECT', 'select')) else 'SELECT' + sql
        reply = self._ask(state, 'refiner', desc_str=state['schema_text'], fk_str=state['foreign_keys'],
                          sql=prompt_sql, sqlite_error=reason,
                          exception_class=execution['exception_class'])
        return {**update, 'sql': parse_final_sql(reply), 'repairs': state['repairs'] + 1,
                'final_sql_checked': False,
                'stop_reason': 'round_limit' if rounds == 3 else ''}

    def run(self, question):
        if not isinstance(question, Question):
            raise TypeError('Only Question is accepted; reference answers are not agent inputs')
        if question.db_id != self.schema.db_id:
            raise ValueError('Question and prepared schema use different databases')
        initial = dict(question=question, run_id=uuid4().hex, rounds=0, repairs=0,
                       feedback={}, checked_sql=None, final_sql_checked=False)
        self._event(initial, 'workflow_started', question=asdict(question),
                    tool_limits=asdict(self.tools.limits), max_output_tokens=self.max_output_tokens,
                    max_prompt_bytes=self.max_prompt_bytes, model=self.model_name,
                    semantic_review=False, output_type_check=False)
        state = self.graph.invoke(initial)
        result = ReproductionResult(state['sql'], state['selector_invoked'], state['repairs'],
                                    state['rounds'], state['final_sql_checked'], state['checked_sql'],
                                    state['feedback'], state['stop_reason'])
        self._event(state, 'workflow_finished', **asdict(result))
        return result


def main():
    parser = argparse.ArgumentParser(description='Offline MAC-SQL replay; no model provider is called')
    parser.add_argument('--upstream-dir', type=Path, required=True)
    parser.add_argument('--tables-json', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--db-id', required=True)
    parser.add_argument('--question', required=True)
    parser.add_argument('--evidence', default='')
    parser.add_argument('--responses', type=Path, required=True, help='JSON array of recorded reply strings')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    checkout = Path(__file__).resolve().parents[3]
    destination = args.output_dir.resolve()
    if destination == checkout or checkout in destination.parents:
        parser.error('Replay artifacts must be outside the source checkout')
    if destination.exists():
        parser.error('Choose a new output directory to preserve existing artifacts')
    entries = json.loads(args.tables_json.read_text(encoding='utf8'))
    entry = next(item for item in entries if item['db_id'] == args.db_id)
    replies = iter(json.loads(args.responses.read_text(encoding='utf8')))
    tools = SQLiteTools({args.db_id: args.database}, ToolLimits(timeout_seconds=120))
    schema = prepare_schema(tools, entry)

    def replay(**kwargs):
        return ModelReply(next(replies), 0, 0, simulated=True)

    agent = OriginalMacSQLAgent(tools=tools, model=replay, model_name='offline-replay',
                               schema=schema, upstream_dir=args.upstream_dir,
                               trace=JsonlTrace(destination / 'trace.jsonl'))
    result = agent.run(Question('offline-replay', args.db_id, args.question, args.evidence))
    (destination / 'result.json').write_text(
        json.dumps({'simulated': True, **asdict(result)}, ensure_ascii=False, indent=2), encoding='utf8')
    print(f'Offline replay finished: {result.stop_reason}; no accuracy claim')


if __name__ == '__main__':
    main()
