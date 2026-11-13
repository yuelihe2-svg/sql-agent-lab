"""Audit final projections and apply an evidence-grounded, projection-only wrapper.

The model interprets requested outputs. Code validates its mapping and preserves
the inner query; neither citations nor these checks prove semantic correctness.
No gold answer, model name or benchmark ID is available here.
"""
import sqlglot
from sqlglot import exp
from .review_quotes import quote_in_source
from .semantic_guard import RevisionRejected, signature


def projection_inventory(sql):
    try:
        statements = sqlglot.parse(sql, read='sqlite')
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise ValueError('Only a single outer SELECT is supported')
        query = statements[0]
        if any(isinstance(e, exp.Star) or isinstance(e, exp.Column) and e.is_star
               for e in query.expressions):
            raise ValueError('Expand SELECT * explicitly before column review')
        return {'status': 'available', 'columns': [
            {'position': i, 'expression': e.sql(dialect='sqlite'), 'name': e.alias_or_name}
            for i, e in enumerate(query.expressions, 1)]}
    except (sqlglot.errors.ParseError, ValueError) as error:
        return {'status': 'unsupported', 'columns': [], 'reason': str(error)}


def audit_output_columns(sql, question, report):
    inventory = projection_inventory(sql)
    result = {**inventory, 'status': 'not_checked', 'requested': [],
              'basis': 'model interpretation checked against source text; not a correctness proof'}
    if inventory['status'] != 'available':
        result['status'] = 'unsupported'
        return result
    contract = report.get('output_contract')
    if not isinstance(contract, dict):
        result['reason'] = 'Saved reply has no per-column output contract; a generic output pass is insufficient'
        return result
    sources = {'question': question.question, 'evidence': question.evidence or ''}
    try:
        requested, columns = contract.get('requested'), contract.get('columns')
        if not isinstance(requested, list) or not 1 <= len(requested) <= 20:
            raise ValueError('Supply 1–20 requested outputs with source quotes')
        ids = set()
        for item in requested:
            if not isinstance(item, dict): raise ValueError('Requested output must be an object')
            rid, source, quote = item.get('id'), item.get('source'), item.get('quote')
            if not isinstance(rid, str) or not rid or rid in ids:
                raise ValueError('Requested outputs need unique IDs')
            if source not in sources or not isinstance(quote, str) or not quote_in_source(quote, sources[source]):
                raise ValueError('Requested output quote is absent from question/evidence')
            if not isinstance(item.get('description'), str) or not item['description'].strip():
                raise ValueError('Describe each requested output explicitly')
            ids.add(rid)
        if not any(i['source'] == 'question' for i in requested):
            raise ValueError('Output contract must cite the actual question')
        result['requested'] = requested
        if not isinstance(columns, list) or len(columns) != len(inventory['columns']):
            raise ValueError('Review every actual SELECT column exactly once')
        checked, mapped, seen = [], set(), set()
        for item in columns:
            if not isinstance(item, dict): raise ValueError('Column review must be an object')
            pos = item.get('position')
            if type(pos) is not int or not 1 <= pos <= len(columns) or pos in seen:
                raise ValueError('Column positions must uniquely match the actual SELECT')
            actual = inventory['columns'][pos-1]
            if not isinstance(item.get('expression'), str) or signature(item['expression']) != signature(actual['expression']):
                raise ValueError('Column expression does not match the actual SELECT')
            disposition, refs = item.get('disposition'), item.get('requested')
            if disposition not in ('required', 'extra', 'uncertain') or not isinstance(refs, list):
                raise ValueError('Use required/extra/uncertain and requested output IDs')
            if any(not isinstance(r, str) or r not in ids for r in refs) or len(set(refs)) != len(refs):
                raise ValueError('Unknown or duplicate requested output ID')
            if not isinstance(item.get('reason'), str) or not item['reason'].strip():
                raise ValueError('Explain each column disposition')
            if disposition == 'required' and not refs:
                raise ValueError('Required columns must map to a requested output')
            if disposition == 'extra':
                if refs: raise ValueError('A requested output cannot simultaneously be extra')
                basis = item.get('basis', {})
                if (not isinstance(basis, dict) or basis.get('source') not in sources
                    or not isinstance(basis.get('quote'), str)
                    or not quote_in_source(basis['quote'], sources[basis['source']])):
                    raise ValueError('An extra-column decision needs a question/evidence quote')
            if disposition == 'required': mapped.update(refs)
            checked.append({**actual, **item}); seen.add(pos)
        result['columns'] = sorted(checked, key=lambda x: x['position'])
        result['missing_requested'] = sorted(ids-mapped)
        extras = [c['position'] for c in checked if c['disposition'] == 'extra']
        result['extra_positions'] = sorted(extras)
        result['keep_positions'] = [c['position'] for c in result['columns'] if c['disposition'] == 'required']
        if any(c['disposition'] == 'uncertain' for c in checked):
            result.update(status='ambiguous', reason='At least one column remains uncertain; preserve SQL')
        elif ids-mapped:
            result.update(status='mismatch', reason='Requested output is not mapped to a retained column')
        elif extras:
            result.update(status='mismatch', reason='Model identified extra returned columns; removal requires a guarded revision')
        else:
            result.update(status='matched', reason='All columns mapped; completeness of model interpretation is not proved')
    except (ValueError, TypeError) as error:
        result.update(status='invalid', reason=str(error))
    return result


def project_required_columns(sql, question, report):
    """Return only mapped columns, preserving the exact inner query AST and order."""
    audit = audit_output_columns(sql, question, report)
    if (audit['status'] != 'mismatch' or not audit.get('extra_positions')
        or audit.get('missing_requested') or not audit.get('keep_positions')):
        raise RevisionRejected('unsafe_output_projection', 'Need unambiguous extra columns and all requested outputs retained')
    if report.get('projection_only') is not True or report.get('action') != 'revise' or report.get('changes') or report.get('sql'):
        raise RevisionRejected('mixed_output_revision', 'Projection-only repair cannot include other SQL edits')
    failed = {k for k, v in report['checks'].items() if v['status'] == 'fail'}
    if failed != {'output'} or any(v['status'] == 'uncertain' for v in report['checks'].values()):
        raise RevisionRejected('mixed_output_revision', 'Only output may fail for a projection-only repair')
    failed_requirements = [r for r in report.get('requirements', []) if r['status'] == 'fail']
    if not failed_requirements or any(r['check'] != 'output' for r in failed_requirements):
        raise RevisionRejected('missing_output_requirement', 'Identify the failed output requirement')
    query = sqlglot.parse_one(sql, read='sqlite')
    names = [c['name'] for c in audit['columns']]
    if any(not n for n in names) or len(set(n.casefold() for n in names)) != len(names):
        raise RevisionRejected('ambiguous_output_names', 'Wrapper needs explicit unique output names/aliases')
    alias = '_agentopt_output'
    def outer_column(index):
        return exp.column(names[index], table=alias, quoted=True)
    outer = exp.select(*[outer_column(i-1) for i in audit['keep_positions']]).from_(query.copy().subquery(alias))
    order = query.args.get('order')
    rewrites = []
    if order:
        terms = []
        for ordered in order.expressions:
            term = ordered.this
            matches = []
            for i, expression in enumerate(query.expressions):
                value = expression.this if isinstance(expression, exp.Alias) else expression
                if (signature(term.sql(dialect='sqlite')) == signature(value.sql(dialect='sqlite'))
                    or isinstance(term, exp.Column) and not term.table and term.name.casefold() == names[i].casefold()):
                    matches.append(i)
            if isinstance(term, exp.Literal) and term.is_int:
                matches = [int(term.this)-1] if 1 <= int(term.this) <= len(names) else []
            if len(matches) != 1:
                raise RevisionRejected('unsupported_output_order', 'Outer ordering must map unambiguously to an existing inner output')
            replacement = ordered.copy(); replacement.set('this', outer_column(matches[0])); terms.append(replacement)
            rewrites.append({'before': ordered.sql(dialect='sqlite'), 'after': replacement.sql(dialect='sqlite')})
        outer.set('order', exp.Order(expressions=terms))
    return outer.sql(dialect='sqlite'), {**audit, 'repair': 'outer_projection',
        'ordering': rewrites, 'inner_query_preserved': True,
        'expected_columns': len(audit['keep_positions'])}
