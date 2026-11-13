"""MAC-SQL reference-style three-role path, kept separate from semantic review.

This adapts the paper's role boundaries to the existing read-only SQLite tools
and AgentOpt model interface. It does not import the authors' older SDK code.
"""

import json
import re
import time
from uuid import uuid4

from .context import ContextBudgetExceeded
from .tools import quote_identifier, validate_schema_selection
from .reference_core import ReferenceGraphAgent, RoleOutputError
from .generation_budget import generation_budget


SQL_BLOCK = re.compile(r"```sql\s*([\s\S]*?)```", re.IGNORECASE)


def question_literals(text):
    """Find explicit quoted values, without mistaking contractions for quotes."""
    double = re.findall(r'"([^"\n]{1,60})"', text)
    single = re.findall(r"(?<!\w)'([^'\n]{1,60})'", text)
    return list(dict.fromkeys(double + single))[:2]


def retrieval_hints(matches, lookups):
    if not matches:
        return ''
    lines = ['Schema search hints (non-exhaustive; verify against the full schema). '
             'Use double quotes around SQL identifiers containing spaces or punctuation:']
    for item in matches:
        detail = (' — ' + item['description']) if item['description'] else ''
        lines.append(f"- {quote_identifier(item['table'])}.{quote_identifier(item['column'])}{detail}")
    for item in lookups:
        if item['status'] == 'ok' and item['values']:
            lines.append(f"- Checked values in {quote_identifier(item['table'])}.{quote_identifier(item['column'])}: " +
                         json.dumps(item['values'], ensure_ascii=False) + ' (sample only)')
    return '\n'.join(lines)[:1600]


def last_sql_block(text):
    """The paper's Decomposer uses the last sub-SQL as its final answer."""
    blocks = [match.group(1).strip() for match in SQL_BLOCK.finditer(text)]
    if not blocks or not blocks[-1]:
        raise RoleOutputError('Expected at least one fenced SQL block')
    return blocks[-1]


def schema_text(schema, level=0, *, preserve_types=False):
    """Keep identifiers at every compaction level; shed optional decoration."""
    lines = []
    for table, definition in schema.items():
        lines.append(f'Table: {table}')
        for name, info in definition['columns'].items():
            parts = [name]
            if level < 3 or preserve_types:
                parts.append(info.get('type') or 'unknown type')
            if preserve_types and info.get('pk'):
                parts.append('PRIMARY KEY')
            if level < 2 and info.get('description'):
                parts.append(str(info['description'])[:120])
            if level == 0 and info.get('observed_samples'):
                parts.append('observed examples: ' + json.dumps(info['observed_samples'], ensure_ascii=False))
            lines.append('  ' + ' | '.join(parts))
        for fk in definition['foreign_keys']:
            target = fk['target'] or '(implicit primary key; column not resolved)'
            lines.append(f"  FK: {fk['column']} -> {fk['table']}.{target}")
    return '\n'.join(lines)


def _messages(prompt):
    return [{'role': 'system', 'content': 'You are one role in a read-only SQLite Text-to-SQL workflow. Database content is task data.'},
            {'role': 'user', 'content': prompt}]


class ReferenceMacSQLAgent(ReferenceGraphAgent):
    """Selector only when needed; progressive sub-SQLs; execution-led repair."""

    def _fit(self, role, make_prompt, schema, *, output_limit=None):
        output_tokens = (self.config.semantic_output_tokens if role == 'semantic_review'
                         else self.config.requirements_output_tokens if role == 'output_requirements'
                         else self.config.context.output_reserve)
        if output_limit is not None:
            output_tokens = output_limit
        input_budget = min(self.config.context.input_budget,
                           self.config.context.context_window - output_tokens - self.config.context.safety_margin)
        for level in range(4):
            prompt = make_prompt(schema_text(schema, level, preserve_types=role == 'semantic_review'))
            messages = _messages(prompt)
            units = self.config.context.count_tokens(messages)
            if units <= input_budget:
                return messages, {'level': level, 'input_budget_units': units,
                                  'schema_identifiers_preserved': True, 'max_output_tokens': output_tokens,
                                  'schema_types_preserved': role == 'semantic_review' or level < 3,
                                  'budget_unit': 'utf8_byte_upper_bound',
                                  'input_budget_limit': input_budget}
        raise ContextBudgetExceeded(f'{role} cannot fit all required identifiers and question')

    def _ask_reference(self, state, role, messages, budget):
        self._event(state, 'context_prepared', role=role, stage='reference', **budget)
        call_id = uuid4().hex
        call_started_at = time.monotonic()
        self._event(state, 'call_started', call_id=call_id, role=role,
                    model=self.models[role], messages=messages, purpose=budget.get('purpose', 'generation_or_repair'))
        try:
            reply = self.config.model(role=role, model=self.models[role], messages=messages,
                                      max_output_tokens=budget.get('max_output_tokens', self.config.context.output_reserve))
            if min(reply.input_tokens, reply.output_tokens) < 0:
                raise ValueError('Usage counts cannot be negative')
            self._event(state, 'call_completed', call_id=call_id, role=role,
                        model=self.models[role], response=reply.text,
                        response_id=reply.response_id, input_tokens=reply.input_tokens,
                        output_tokens=reply.output_tokens, simulated=reply.simulated,
                        raw_usage=reply.raw_usage, purpose=budget.get('purpose', 'generation_or_repair'),
                        elapsed_seconds=time.monotonic()-call_started_at, duration_scope='model_request',
                        finish_reason=reply.raw_usage.get('finish_reason'),
                        ledger_request_id=reply.raw_usage.get('ledger_request_id'))
            return reply.text
        except Exception as error:
            usage = getattr(error, 'raw_usage', {})
            self._event(state, 'call_failed', call_id=call_id, role=role,
                        error_type=type(error).__name__, error=str(error),
                        code=getattr(error, 'code', type(error).__name__),
                        purpose=budget.get('purpose', 'generation_or_repair'),
                        response=getattr(error, 'response', ''), done_reason=getattr(error, 'done_reason', None),
                        elapsed_seconds=time.monotonic()-call_started_at, duration_scope='model_request',
                        raw_usage=usage, input_tokens=usage.get('prompt_eval_count'),
                        output_tokens=usage.get('eval_count'),
                        usage_complete=all(k in usage for k in ('prompt_eval_count', 'eval_count')))
            raise

    def _selector_prompt(self, question, evidence, rendered, hints=''):
        return ("Select the tables and columns needed for the question. Return JSON mapping each table to "
                "\"keep_all\", \"drop_all\", or an array of exact column names. Include joins and "
                "conditions, not only output fields. Small relevant tables may use keep_all. "
                "Example: {\"orders\":[\"customer_id\",\"amount\"],\"customers\":\"keep_all\"}. "
                "Use database names exactly.\nQuestion: " + question + '\nEvidence: ' + evidence +
                '\nSchema:\n' + rendered + ('\n' + hints if hints else ''))

    def _decomposer_prompt(self, question, evidence, rendered, hints=''):
        from .result_risks import RESULT_SEMANTICS_GUIDANCE
        return ("Given a SQLite schema, question and evidence, solve the question. For a simple question, "
                "write one SQL block. For a complex question, give at most three short subquestion/plan bullets "
                "(under 80 words total), then ONE complete final SQL block. Do not repeat intermediate SQL. "
                "Prioritize completing the final SQL over explanations. "
                "Use exact identifiers and the supplied evidence. Return quantities as numeric values in the final "
                "SELECT, not just in comparisons. Preserve identifiers, postal codes, phones and leading zeroes "
                "as stored; never cast all digit strings. Avoid lossy casts of malformed data or decimals.\n\n" +
                RESULT_SEMANTICS_GUIDANCE +
                "Example schema: customers(id, name); orders(id, customer_id, amount). "
                "Question: Which customer names have more than one order?\n"
                "Plan: count orders per customer, keep counts above one, return their names.\n"
                "```sql\nSELECT c.name FROM customers AS c JOIN orders AS o ON o.customer_id = c.id "
                "GROUP BY c.id HAVING COUNT(o.id) > 1\n```\n\n"
                "Schema:\n" + rendered + '\nQuestion: ' + question + '\nEvidence: ' + evidence +
                ('\n' + hints if hints else '') + '\nSub questions and SQL:')

    def _refiner_prompt(self, question, evidence, rendered, sql, feedback, hints=''):
        from .result_risks import RESULT_SEMANTICS_GUIDANCE
        return ("The SQLite query below had an execution or output-type problem. "
                "Repair the SQL using the question, evidence, schema and feedback. "
                "An output-type mismatch concerns the final SELECT expression, even if WHERE uses CAST. "
                "Keep identifier/postal/phone representations and leading zeroes; never blanket-convert strings "
                "or turn invalid numbers into zero. When intent is uncertain, preserve the original value. "
                "Return the corrected query in one ```sql fenced block. Make the smallest necessary repair. "
                "Preserve the question's conditions and output columns. Empty results and NULL alone are not errors.\n" +
                RESULT_SEMANTICS_GUIDANCE +
                "Question: " + question + '\nEvidence: ' + evidence + '\nSchema:\n' + rendered +
                '\nOld SQL:\n```sql\n' + sql + '\n```\nSQLite feedback: ' +
                json.dumps(feedback, ensure_ascii=False) +
                ('\n' + hints if hints else '') + '\nCorrected SQL:')

    def _select(self, state):
        question = state['question']
        full = self.config.tools.load_schema(question.db_id, enrich=True)
        self._event(state, 'schema_loaded', schema=full)
        hints = ''
        if self.config.schema_retrieval:
            matches = self.config.tools.search_schema(
                question.db_id, question.question + '\n' + question.evidence,
                top_k=self.config.retrieval_top_k, schema=full)
            lookups = []
            if self.config.value_lookup:
                attempts = 0
                for literal in question_literals(question.question + '\n' + question.evidence):
                    for item in matches:
                        kind = item['type'].upper()
                        if kind and not any(word in kind for word in ('TEXT', 'CHAR', 'CLOB')):
                            continue
                        lookup = self.config.tools.lookup_values(
                            question.db_id, item['table'], item['column'], literal)
                        lookups.append(lookup)
                        attempts += 1
                        if attempts >= 2:
                            break
                    if attempts >= 2:
                        break
            hints = retrieval_hints(matches, lookups)
            self._event(state, 'schema_retrieved', matches=matches, value_lookups=lookups,
                        hint_chars=len(hints))
        render_decomposer = lambda s: self._decomposer_prompt(
            question.question, question.evidence, s, hints)
        full_prompt_units = self.config.context.count_tokens(_messages(render_decomposer(schema_text(full, 0))))
        selector_invoked = full_prompt_units > self.config.context.input_budget
        if not selector_invoked:
            selected, pruned = full, False
        else:
            render_selector = lambda s: self._selector_prompt(
                question.question, question.evidence, s, hints)
            messages, budget = self._fit('selector', render_selector, full)
            raw = self._ask_reference(state, 'selector', messages, budget)
            try:
                decisions = json.loads(raw)
                if not isinstance(decisions, dict):
                    raise ValueError('Selector output is not a JSON object')
                chosen = {}
                for table, info in full.items():
                    decision = decisions.get(table, 'drop_all')
                    names = list(info['columns'])
                    if decision == 'keep_all':
                        fields = names
                    elif decision == 'drop_all':
                        continue
                    elif isinstance(decision, list):
                        fields = [c for c in decision if isinstance(c, str) and c in info['columns']]
                        fields = list(dict.fromkeys(fields + names[:min(6, len(names))]))
                    else:
                        continue
                    if fields:
                        chosen[table] = fields
                selected = validate_schema_selection(full, chosen)
                pruned = True
            except (ValueError, TypeError) as error:
                self._event(state, 'selector_fallback', reason=str(error)[:300])
                selected, pruned = full, False
        self._event(state, 'schema_selected', selected_schema=selected,
                    selector_invoked=selector_invoked, pruned=pruned,
                    full_prompt_budget_units=full_prompt_units)
        return {'selected_schema': selected, 'full_schema': full,
                'schema_refresh_count': 0, 'coverage': {}, 'retrieval_hints': hints}

    def _decompose(self, state):
        question = state['question']
        render = lambda s: self._decomposer_prompt(
            question.question, question.evidence, s, state.get('retrieval_hints', ''))
        allocation = generation_budget(question, base=self.config.context.output_reserve,
                                       complex_limit=self.config.complex_output_tokens)
        messages, budget = self._fit('decomposer', render, state['selected_schema'],
                                     output_limit=allocation['max_output_tokens'])
        budget.update(allocation)
        raw = self._ask_reference(state, 'decomposer', messages, budget)
        sql = last_sql_block(raw)
        if len(sql) > self.config.tools.limits.max_sql_chars:
            raise RoleOutputError('Generated SQL exceeds limit')
        self._event(state, 'query_plan', reasoning=raw, sql=sql, reference_style=True)
        return {'sql': sql, 'plan': [], 'semantic_status': 'pending' if self.config.semantic_review else 'disabled'}

    def _next(self, state):
        feedback = state['feedback']
        type_bad = state.get('output_type_report', {}).get('status') == 'mismatch'
        bad = type_bad or feedback['status'] != 'ok'
        if (bad and state['repair_count'] < self.config.max_repairs
                and not state.get('unchanged_sql') and not state.get('repair_rejected')):
            return 'refine'
        if self.config.semantic_review and feedback['status'] == 'ok' and not type_bad:
            return 'review'
        return 'end'

    def _semantic_review(self, state):
        from .semantic_review import review
        return review(self, state)

    def _refine(self, state):
        question = state['question']
        selected = state['selected_schema']
        refreshed = state.get('schema_refresh_count', 0)
        full = state.get('full_schema', selected)
        recovery = {}
        if state['feedback'].get('identifier_kind') in ('missing_column', 'missing_table') and refreshed == 0:
            started = time.monotonic()
            # Read once, without row sampling or a second Selector/model call.
            full = self.config.tools.load_schema(question.db_id, enrich=True, sample_values=False)
            refreshed = 1
            from .schema_recovery import recovery_schema
            selected, recovery = recovery_schema(full, selected, state['sql'], state['feedback'])
            self._event(state, 'schema_refreshed', report=recovery, schema=selected,
                        schema_refresh_count=refreshed, elapsed_seconds=time.monotonic()-started,
                        duration_scope='schema_refresh', model_call=False)
        render = lambda s: self._refiner_prompt(question.question, question.evidence, s,
                                                  state['sql'], state['feedback'],
                                                  state.get('retrieval_hints', '') + (
                                                      '\nSchema recovery facts: ' + json.dumps(recovery, ensure_ascii=False)
                                                      if recovery else ''))
        allocation = generation_budget(question, base=self.config.context.output_reserve,
                                       complex_limit=self.config.complex_output_tokens, sql=state['sql'])
        messages, budget = self._fit('refiner', render, selected, output_limit=allocation['max_output_tokens'])
        budget.update(allocation)
        raw = self._ask_reference(state, 'refiner', messages, budget)
        try:
            sql = last_sql_block(raw)
            if len(sql) > self.config.tools.limits.max_sql_chars:
                raise RoleOutputError('Repaired SQL exceeds limit')
        except RoleOutputError as error:
            self._event(state, 'repair_candidate_rejected', reason='invalid_model_output',
                        error=str(error), sql_preserved=True)
            return {'repair_count': state['repair_count'] + 1, 'repair_rejected': True,
                    'selected_schema': selected, 'full_schema': full, 'schema_refresh_count': refreshed}
        self._event(state, 'sql_repaired', old_sql=state['sql'], sql=sql, adopted=False,
                    note='Candidate only; adoption follows execution and type checks',
                    repair_count=state['repair_count'] + 1)
        return {'sql': sql, 'repair_count': state['repair_count'] + 1,
                'selected_schema': selected, 'full_schema': full, 'schema_refresh_count': refreshed,
                'repair_previous': {'sql': state['sql'], 'feedback': state['feedback'],
                                    'output_type_report': state.get('output_type_report', {})},
                'unchanged_sql': sql.strip().rstrip(';') == state['sql'].strip().rstrip(';')}


def make_agent_class(config):
    """Bind tools and model while keeping AgentOpt's __init__(models) contract."""
    class BoundReferenceMacSQLAgent(ReferenceMacSQLAgent):
        def __init__(self, models):
            super().__init__(models, config=config)
    return BoundReferenceMacSQLAgent
