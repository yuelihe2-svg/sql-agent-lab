"""One bounded, requirement-grounded Refiner audit without reference answers."""
import json
import time
from .semantic_guard import RevisionRejected, apply_revision, evidence_context
from .review_contract import CHECKS, ReviewProtocolError, parse_report, validate_requirements, check_revision_requirements
from .output_columns import projection_inventory, audit_output_columns, project_required_columns
from .output_requirements import extract, bind_report, assess_requirements
from .review_context import select_review_schema

MARKER = 'SEMANTIC_REVIEW_V8'
CONTRACT = 'review_with_scoped_output_uncertainty_v8_trace2'


def review(agent, state):
    started = time.monotonic()
    try:
        frozen = extract(agent, state)
    except Exception as error:
        code = getattr(error, 'code', type(error).__name__)
        elapsed = time.monotonic()-started
        agent._event(state, 'output_requirements_error', code=code, error=str(error), sql_preserved=True,
                     elapsed_seconds=elapsed, duration_scope='requirements_only', sql_review_ran=False)
        return {'semantic_status': 'requirements_unverified', 'semantic_review_count': 0,
                'output_requirements_status': 'failed', 'output_requirements_count': 1,
                'semantic_metrics': {'outcome': code, 'sql_changed': False, 'elapsed_seconds': elapsed,
                    'duration_scope': 'requirements_only', 'requirements_seconds': elapsed,
                    'sql_review_seconds': None, 'sql_review_ran': False, 'total_review_seconds': elapsed}}
    assessment = assess_requirements(frozen.spec)
    if assessment['risks'] or frozen.protocol_warnings:
        agent._event(state, 'semantic_review_scope_warning', assessment=assessment,
                     protocol_warnings=list(frozen.protocol_warnings),
                     note='Continue SQL inspection; uncertain output interpretation cannot authorize column removal')
    result = _review_sql(agent, state, frozen)
    result.update(output_requirements=frozen.spec, output_requirements_digest=frozen.digest,
                  output_requirements_assessment=assessment,
                  output_requirements_protocol_warnings=list(frozen.protocol_warnings),
                  output_requirements_count=1, output_requirements_status=frozen.spec['status'])
    result.setdefault('semantic_metrics', {})['total_review_seconds'] = time.monotonic()-started
    result['semantic_metrics']['requirements_seconds'] = max(0,
        result['semantic_metrics']['total_review_seconds'] - result['semantic_metrics'].get('elapsed_seconds', 0))
    return result


def _review_sql(agent, state, frozen):
    started = time.monotonic()
    question, before = state['question'], state['sql']
    relevant, schema_scope = select_review_schema(state['full_schema'], before)
    schema, sources, catalog = evidence_context(question, relevant, before)
    agent._event(state, 'review_context_selected', report=schema_scope,
                 documented_items=sum(c['kind'] in ('description', 'enum_meanings') for c in catalog))
    report = None
    output_audit = projection_inventory(before)
    agent._event(state, 'output_columns_inventory', report=output_audit)
    def finish(updates, outcome):
        metrics = {'outcome': outcome, 'sql_changed': updates.get('sql', before) != before,
                   'revision_proposed': bool(report and report.get('action') == 'revise'),
                   'format_normalized': bool(report and report.get('format_normalized')),
                   'elapsed_seconds': time.monotonic() - started}
        metrics['sql_review_seconds'] = metrics['elapsed_seconds']
        metrics['duration_scope'] = 'sql_review_only_excludes_requirements'
        updates['semantic_metrics'] = metrics
        updates['output_columns_report'] = output_audit
        agent._event(state, 'semantic_review_finished', **metrics,
                     semantic_status=updates['semantic_status'])
        return updates
    diagnostics = agent.config.tools.diagnose_sql_readonly(question.db_id, before)
    for finding in diagnostics.get('findings', []):
        if finding['status'] in ('observed_null', 'no_null_found'):
            sources[finding['id']] = finding['detail']
            catalog.append({'source': finding['id'], 'kind': 'readonly_probe', 'text': finding['detail']})
    agent._event(state, 'semantic_diagnostics', report=diagnostics)
    agent._event(state, 'semantic_review_started', role='refiner', sql=before,
                 contract=CONTRACT, evidence_catalog=catalog,
                 note='One audit; report structure and citations are checked, not semantic completeness or correctness')
    def prompt(rendered):
        return (MARKER + '\nAudit this SQLite SQL against every clause of the question AND evidence. '
                'Successful execution is not semantic correctness. Split independent conditions into separate '
                'requirements: requested output, each filter/entity/time bound, joins, counting unit and threshold, '
                'sorting/ties, limit. Distinguish counting people from counting their related rows. '
                'Do not invent a time requirement or narrower category. Sorting by a metric does NOT mean '
                'the metric must be an output column. Preserve equivalent SQL. Empty results and NULL can be legitimate. '
                'Read the actual diagnostic facts; unknown is not absence. Do not infer absence from previews. '
                'Return compact JSON only, no explanations outside JSON. Use this structure:\n'
                '{"action":"accept|revise|uncertain","requirements":['
                '{"id":"r1","source":"question|evidence","quote":"exact source clause",'
                '"check":"output|filters|joins|aggregation|ordering|limit","status":"pass|fail|uncertain",'
                '"sql_fragment":"actual SQL fragment, or empty for missing/uncertain requirement",'
                '"reason":"brief reason"}],"checks":{'
                '"output":{"status":"pass|fail|uncertain","reason":"brief"},'
                '"filters":{},"joins":{},"aggregation":{},"ordering":{},"limit":{}},"changes":[], '
                '"output_contract":{"columns":[{"position":1,"expression":"exact inventory expression",'
                '"disposition":"required|extra|uncertain","requested":["o1"],"reason":"brief",'
                '"basis":{"source":"question|evidence","quote":"basis for extra classification"}}]},'
                '"projection_only":false}\n'
                'Requested outputs were extracted in a separate request without this SQL and are frozen below. '
                'Do not redefine or echo output_contract.requested; use the fixed IDs. If they seem ambiguous or '
                'incorrect, choose uncertain; do not rewrite them to fit this SQL. '
                'Frozen means an immutable interpretation for comparison, not an authoritative answer. '
                'Output ambiguity does not stop the audit: independently check filters, joins, aggregation, ordering '
                'and limits. Mark only the affected output requirements/columns uncertain and report concrete '
                'defects elsewhere. An unspecified entity representation alone does not make the current SQL wrong. '
                'Do not force a name-only or ID-only representation. If frozen outputs conflict with the question, '
                'explain the conflict, mark output uncertain and continue the other checks. '
                'Check the interpretation against the original question; a true quote need not support its conclusion. '
                'For each proposed removal explain what the column represents, why it is internal-only, '
                'and which retained expressions still represent the requested entity and attributes. '
                'If another reasonable reading needs that column, choose uncertain. Never resolve unclear '
                'entity identity by arbitrarily preferring names or IDs. Column removal is suggestion-only: '
                'it cannot replace the submitted SQL in this version. '
                'Classify EVERY inventory column exactly once. Required columns map to frozen requested IDs; '
                'extra columns have requested:[] and a source quote plus reason explaining their internal-only purpose. '
                'A join key, grouping key, or ordering metric is not automatically a requested output. '
                'If the question explicitly asks for both names and values/IDs, retain all of them. '
                'Do not infer output count solely from singular/plural words. Ambiguity requires uncertain. '
                'An output pass requires all requested outputs mapped and no extra/uncertain columns. '
                'Fill ALL six checks with status/reason. Identify substantive requirements from question/evidence '
                'as short atomic clauses with IDs r1,r2,...; do not quote narrative filler or repeat every word. '
                'Check that no substantive condition was missed; text coverage alone cannot prove completeness. '
                'Passed requirements must point to real SQL fragments; prefer short exact fragments over ellipses. '
                'Quote supplied text; whitespace and typographic quotation differences are allowed, not invented facts. '
                'For nonapplicable overall checks use status="pass" and reason="not required"; status must '
                'always be pass, fail or uncertain. Requirements come only from the Question/Evidence fields; '
                'documented definitions dN, schema foreign keys schema_fk_N and diagnostic facts tN may be supporting items with their actual source ID, '
                'not extra user requirements. Do not label documentation as question/evidence. '
                'Accept only if all requirements/checks pass; '
                'uncertain retains original SQL. Revise only for concrete defects with no unresolved uncertainty. '
                'For revise, provide at most 6 local changes. Each needs requirements:[failed IDs], checks:[failed '
                'check names], before:unique original fragment, after:replacement, requirement_quote:exact source '
                'quote, evidence:[{source:question|evidence|dN|tN,quote:exact source quote}]. Every failed check and '
                'failed requirement must link to an edit. Combine checks on the same clause; do not overlap patches. '
                'Make a real change that resolves the cited defect, preserving SQL fragments for passed requirements. '
                'If ONLY output has a concrete extra-column defect, use action=revise, projection_only=true, '
                'changes:[] and an output fail requirement. The program will wrap the unchanged query, returning '
                'only required columns while preserving internal computations and ordering. Do not write wrapper SQL. '
                'For other repairs use projection_only=false and local changes; do not combine them with column removal. '
                'Ambiguous output shape requires uncertain. '
                'Do not invent identifiers or literal values. No full sql field. Keep reasons very short; '
                'complete the JSON within ' + str(agent.config.semantic_output_tokens - 150) + ' tokens.\nQuestion: ' + question.question + '\nEvidence: ' + question.evidence +
                '\nSchema (identifiers/types):\n' + rendered +
                '\nSchema scope and omitted table inventory:\n' + json.dumps(schema_scope, ensure_ascii=False) +
                '\nFrozen logical output requirements (do not change):\n' + frozen.serialized +
                '\nOutput interpretation risks (inspection allowed, deletion not authorized):\n' +
                json.dumps(assess_requirements(frozen.spec), ensure_ascii=False) +
                '\nExtraction protocol warnings:\n' + json.dumps(frozen.protocol_warnings) +
                '\nDocumented evidence (samples are not authority):\n' + json.dumps(catalog, ensure_ascii=False) +
                '\nReadonly diagnostics:\n' + json.dumps(diagnostics, ensure_ascii=False) +
                '\nActual outer SELECT inventory (program parsed):\n' + json.dumps(output_audit, ensure_ascii=False) +
                '\nSQL:\n' + before + '\nExecution preview (incomplete): ' +
                json.dumps({k: state['feedback'].get(k) for k in ('status', 'columns', 'preview_rows', 'has_more')}, ensure_ascii=False))
    try:
        messages, budget = agent._fit('semantic_review', prompt, schema)
        raw = agent._ask_reference(state, 'refiner', messages, {**budget, 'purpose': 'semantic_review'})
        report = parse_report(raw)
        report = bind_report(report, frozen, question)
        output_audit = audit_output_columns(before, question, report)
        agent._event(state, 'output_columns_checked', report=output_audit)
        if report.get('format_normalized'):
            agent._event(state, 'semantic_format_normalized', normalization=report['format_normalized'])
        coverage = validate_requirements(report, question, before, sources)
        agent._event(state, 'semantic_requirements_checked', report=coverage, requirements=report['requirements'])
    except Exception as error:
        code = getattr(error, 'code', type(error).__name__)
        agent._event(state, 'semantic_review_error', code=code, error_type=type(error).__name__,
                     error=str(error), sql_preserved=True,
                     failure_kind='report_validation' if isinstance(error, ReviewProtocolError) else 'review_runtime')
        return finish({'semantic_status': 'unverified', 'semantic_review_count': 1,
                       'semantic_report': {**(report or {}), 'error': str(error), 'error_code': code,
                            'failure_kind': 'report_validation' if isinstance(error, ReviewProtocolError) else 'review_runtime'}}, code)
    agent._event(state, 'semantic_review_completed', report=report)
    updates = {'semantic_review_count': 1, 'semantic_revision_count': 0, 'semantic_report': report,
               'semantic_validation': coverage,
               'semantic_status': 'accepted' if report['action'] == 'accept' else 'uncertain'}
    assessment = report.get('output_requirements_assessment', {})
    warnings = [f for f in diagnostics.get('findings', []) if f['status'] != 'no_null_found']
    if output_audit['status'] in ('not_checked', 'invalid', 'unsupported', 'ambiguous'):
        updates['semantic_status'] = 'output_unverified'
        return finish(updates, 'output_columns_' + output_audit['status'])
    if output_audit['status'] == 'mismatch' and not report.get('projection_only'):
        updates['semantic_status'] = 'output_mismatch'
        return finish(updates, 'output_columns_unresolved')
    if report['action'] != 'revise':
        if any(r['code'] != 'legacy_output_support_unverified' for r in assessment.get('risks', [])):
            updates['semantic_status'] = 'output_unverified'
            return finish(updates, 'output_interpretation_unresolved')
        if report['action'] == 'accept' and (assessment.get('risks') or coverage['warnings'] or warnings or diagnostics['status'] != 'completed' or diagnostics.get('truncated')):
            updates['semantic_status'] = 'accepted_with_warnings'
        return finish(updates, updates['semantic_status'])
    try:
        projection = report.get('projection_only') is True
        if projection:
            if assessment.get('status') != 'structurally_supported':
                raise RevisionRejected('output_support_unverified', 'Output interpretation lacks support or has unresolved conflicts; preserve SQL')
            candidate, projection_details = project_required_columns(before, question, report)
            # Generation is not adoption. Never execute/submit a destructive projection automatically.
            suggestion = {'sql': candidate, 'adopted': False, 'executed': False,
                          'reason': 'column_removal_suggestion_only', 'report': projection_details,
                          'correctness': 'not_evaluated'}
            agent._event(state, 'output_columns_suggested', **suggestion)
            updates.update(output_projection_suggestion=suggestion, semantic_status='projection_suggested')
            return finish(updates, 'column_removal_suggestion_only')
        else:
            if any(r['code'] != 'legacy_output_support_unverified' for r in assessment.get('risks', [])):
                raise RevisionRejected('output_support_unverified',
                    'Inspection completed, but unresolved output interpretation cannot authorize edits')
            candidate = apply_revision(before, report, question, sources)
            check_revision_requirements(before, candidate, report)
            if agent.config.semantic_suggestion_only:
                suggestion = {'sql': candidate, 'adopted': False, 'executed': False,
                              'reason': 'semantic_suggestion_only', 'correctness': 'not_evaluated'}
                agent._event(state, 'semantic_sql_suggested', **suggestion)
                updates.update(semantic_suggestion=suggestion, semantic_status='semantic_suggested')
                return finish(updates, 'semantic_suggestion_only')
    except RevisionRejected as error:
        agent._event(state, 'semantic_revision_guard', passed=False, code=error.code,
                     reason=str(error), sql_preserved=True)
        updates.update(semantic_status='revision_guard_rejected',
                       semantic_report={**report, 'guard': {'code': error.code, 'reason': str(error)}})
        return finish(updates, error.code)
    agent._event(state, 'semantic_revision_guard', passed=True, code='grounded_edits_applied',
                 changes=report.get('changes', []), sql=candidate,
                 note='Grounding, anchors and edit application checked; semantic correctness is not proved')
    tools = agent.config.tools
    checked = tools.compile_sql_readonly(question.db_id, candidate)
    if checked['status'] == 'ok':
        checked = tools.execute_sql_readonly(question.db_id, candidate)
    type_report = agent._check_output(state, candidate, checked, phase='semantic_revision')
    checked['output_type_check'] = type_report
    adopted = checked['status'] == 'ok' and type_report['status'] != 'mismatch'
    outcome = 'revision_applied_unverified' if adopted else (
        'execution_error' if checked['status'] != 'ok' else 'output_type_error')
    if adopted:
        before_width = state['feedback'].get('output_type_profile', {}).get('total_columns')
        after_width = checked.get('output_type_profile', {}).get('total_columns')
        expected_width = projection_details['expected_columns'] if projection else before_width
        if expected_width is not None and after_width != expected_width:
            adopted, outcome = False, 'output_shape_changed'
        after_diagnostics = tools.diagnose_sql_readonly(question.db_id, candidate)
        agent._event(state, 'semantic_diagnostics', phase='revision', report=after_diagnostics)
        cited_probes = {c['source'] for change in report.get('changes', []) for c in change['evidence']
                        if c['source'].startswith('t')}
        if cited_probes and any(f['status'] == 'observed_null' for f in after_diagnostics['findings']):
            adopted, outcome = False, 'diagnostic_unresolved'
    agent._event(state, 'semantic_revision_checked', old_sql=before, sql=candidate,
                 feedback=checked, adopted=adopted, outcome=outcome)
    if adopted:
        updates.update(sql=candidate, feedback=checked, semantic_status='revised_unverified', semantic_revision_count=1,
                       output_type_report=type_report)
    else:
        updates['semantic_status'] = 'revision_rejected'
    return finish(updates, outcome)
