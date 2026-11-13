"""Local advisory checks, never an answer oracle or an automatic SQL rewrite."""

RESULT_SEMANTICS_GUIDANCE = (
    "Result semantics: separate qualifying entities from the relations being counted. "
    "When every qualifying entity is requested, retain entities with zero related records "
    "using a correlated count or LEFT JOIN and COUNT(related_key), not COUNT(*). "
    "Keep optional-side filters in ON or the aggregate; WHERE may erase zero rows. "
    "If the question requires a match or positive count, retain that restriction. "
    "Choose the counting unit and DISTINCT from the question, not by habit. "
    "For requested existing entity IDs, exclude NULL identity values; NULL is not an ID. "
    "Do not blanket-remove NULL rows: preserve optional attributes, unknown groups, "
    "and missing IDs when explicitly requested. Do not replace missing measurements with zero "
    "unless the question requires it.\n"
)


def inspect_result_risks(sql, feedback, type_report):
    """Inspect AST and already collected storage classes; no SQL/model calls.

    Findings need question interpretation. They neither trigger a repair nor mark
    SQL wrong; a normal execution/type repair can consume them as context.
    """
    report = {'status': 'not_checked', 'findings': [], 'automatic_rewrite': False,
              'triggers_model_call': False, 'scope': 'Structural risks, not semantic correctness'}
    if feedback.get('status') != 'ok':
        return report
    report['status'] = 'checked'
    for column in type_report.get('columns', []):
        count = column.get('storage_classes', {}).get('null', 0)
        if count and column.get('protected_identifier'):
            report['findings'].append({
                'code': 'null_identifier_output', 'column_index': column['index'],
                'column_name': column['name'], 'observed_null_count': count,
                'row_scope': type_report.get('row_scope', 'unknown'),
                'severity': 'advisory',
                'detail': 'NULL observed in identifier lineage. Check whether the question asks for existing '
                          'entity IDs or explicitly retains missing/unknown identities; do not drop NULL blindly.'})
    try:
        from sqlglot import exp, parse_one
        tree = parse_one(sql, read='sqlite')
        for select in tree.find_all(exp.Select):
            # Restrict counts to this SELECT scope, not nested scalar subqueries.
            counts = [n for n in select.find_all(exp.Count) if n.find_ancestor(exp.Select) is select]
            joins = select.args.get('joins') or []
            inner = [j for j in joins if not j.side and j.kind in ('', 'INNER')]
            if counts and inner and select.args.get('group'):
                report['findings'].append({
                    'code': 'grouped_count_inner_join', 'severity': 'advisory',
                    'detail': 'Grouped count uses inner joins. Confirm entity eligibility independently of '
                              'the counted relationship; zero-match entities may be excluded. '
                              'Inner joins are correct when existence/positive count is required.',
                    'join_count': len(inner)})
    except Exception as error:
        report['status'] = 'partially_checked'
        report['analysis_note'] = f'{type(error).__name__}: {error}'[:200]
    return report
