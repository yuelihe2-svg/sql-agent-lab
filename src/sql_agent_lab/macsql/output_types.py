"""Conservative output contracts and observed SQLite storage classes.

SQLGlot is used for analysis only. SQLite still validates/executes the original
SQL. No output value or SQL is automatically cast, and no reference is read.
"""
import re

ALLOWED_TYPES = {'integer', 'number', 'text', 'unknown'}
NUMERIC_TEXT = re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')


def storage_class(value):
    if value is None:
        return 'null'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'real'
    if isinstance(value, bytes):
        return 'blob'
    return 'text'


def semantic_kind(name, info):
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', name).replace('_', ' ').casefold()
    description = ''
    if not info.get('documentation_truncated'):
        description = str(info.get('description', '')).replace('_', ' ').casefold().strip()
    identity_text = text + ' ' + description
    protected = (info.get('pk') or re.search(
        r'\b(id|ids|identifier|identification|code|codes|zip|zipcode|postal|postcode|phone|telephone|isbn|ssn)\b', identity_text))
    if protected:
        return 'identifier'
    # A mention of a numeric concept in prose (e.g. "category of the cost")
    # does not make this column numeric. Only a direct quantity definition helps.
    numeric_text = text
    if re.match(r'^(?:the\s+)?(?:number of|population|inhabitants|headcount|count|age|amount|salary|cost|price|balance|percentage|percent|ratio|rate|average|mean|score|weight|height|distance)\b', description):
        numeric_text += ' ' + description
    if re.search(r'\b(population|inhabitants|headcount|count|counts|number of|age|ages)\b', numeric_text):
        return 'integer'
    if re.search(r'\b(amount|salary|cost|price|balance|percentage|percent|ratio|rate|average|mean|score|weight|height|distance)\b', numeric_text):
        return 'number'
    if re.search(r'\b(name|email|address|description|title)\b', identity_text):
        return 'text'
    return 'unknown'


def infer_contract(sql, schema):
    from sqlglot import exp, parse_one
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.qualify import qualify
    types = {t: {c: info.get('type') or 'UNKNOWN' for c, info in d['columns'].items()}
             for t, d in schema.items()}
    lookup = {(t.casefold(), c.casefold()): info for t, d in schema.items() for c, info in d['columns'].items()}
    names = {(t.casefold(), c.casefold()): (t, c) for t, d in schema.items() for c in d['columns']}
    tree = qualify(parse_one(sql, read='sqlite'), schema=types, dialect='sqlite')
    if not isinstance(tree, exp.Select):
        raise ValueError('Set-operation output lineage requires an explicit positional contract')
    # Unique analysis-only aliases avoid duplicate names and map by position.
    tree.set('expressions', [exp.alias_(e.unalias().copy(), f'__output_{i}', quoted=True)
                             for i, e in enumerate(tree.selects)])

    def source(node):
        column = parse_one(node.name, into=exp.Column, read='sqlite').name
        table = node.expression.name
        info = lookup.get((table.casefold(), column.casefold()), {})
        table, column = names.get((table.casefold(), column.casefold()), (table, column))
        return table, column, info

    def meaning(node):
        if isinstance(node.expression, exp.Table):
            table, column, info = source(node)
            kind = semantic_kind(column, info)
            affinity = str(info.get('type', '')).upper()
            expected = ('text' if any(x in affinity for x in ('CHAR','CLOB','TEXT')) else 'unknown') if kind=='identifier' else kind
            return expected, kind == 'identifier', f'{table}.{column}: {kind}'
        expression = node.expression.unalias()
        while isinstance(expression, (exp.Cast, exp.Paren, exp.Window)):
            expression = expression.this
        # Counting identifiers or measuring text length is a quantity, not an identifier.
        if isinstance(expression, (exp.Count, exp.Length, exp.RowNumber, exp.Rank, exp.DenseRank)):
            return 'integer', False, 'SQL count/length/rank expression'
        # Numeric operators determine their output type. IDs used in CASE
        # predicates or grouping do not turn a sum/ratio into an identifier.
        if isinstance(expression, (exp.Avg, exp.Sum, exp.Round, exp.Div, exp.Add, exp.Sub, exp.Mul)):
            return 'number', False, 'SQL numeric expression'
        children = [meaning(child) for child in node.downstream]
        if any(c[1] for c in children):
            # Do not let an alias such as "amount" defeat identifier protection.
            expected = 'text' if any(c[0]=='text' and c[1] for c in children) else 'unknown'
            return expected, True, 'Identifier/code lineage; preserve its representation'
        if isinstance(expression, exp.Literal):
            return ('text' if expression.is_string else 'number'), False, 'SQL literal'
        if children and len({c[0] for c in children}) == 1:
            # CTEs, aliases, CAST, MIN/MAX and COALESCE retain semantic intent.
            return children[0][0], False, children[0][2]
        return 'unknown', False, 'Insufficient or mixed output semantics'

    results = []
    for i in range(len(tree.selects)):
        node = lineage(f'__output_{i}', tree, schema=types, dialect='sqlite')
        expected, protected, reason = meaning(node)
        origins = []
        unsafe_cast = False
        for item in node.walk():
            if isinstance(item.expression, exp.Table):
                table, column, info = source(item)
                origins.append({'table': table, 'column': column, 'declared_type': info.get('type', '')})
            if protected and expected == 'text':
                for cast in item.expression.find_all(exp.Cast):
                    if cast.to.sql().upper() not in ('TEXT', 'VARCHAR', 'CHAR', 'CLOB'):
                        unsafe_cast = True
        results.append({'index': i, 'expected': expected, 'protected_identifier': protected,
                        'reason': reason, 'sources': origins, 'unsafe_identifier_cast': unsafe_cast})
    return results


def check_output_types(sql, feedback, schema, explicit=()):
    """Return mismatch only with a concrete contract; uncertainty never converts."""
    report = {'status': 'unverified', 'automatic_conversion': False, 'columns': []}
    if feedback.get('status') != 'ok':
        return {**report, 'status': 'skipped', 'reason': 'SQL execution failed'}
    profile = feedback.get('output_type_profile')
    if not profile:
        return {**report, 'reason': 'No raw storage-class profile available'}
    all_columns = len(profile['columns']) == profile['total_columns']
    report.update(scope='complete' if profile['complete'] and all_columns else 'bounded_sample',
                  row_scope='complete' if profile['complete'] else 'bounded_sample',
                  column_scope='all_columns' if all_columns else 'partial_columns',
                  checked_columns=len(profile['columns']), total_columns=profile['total_columns'],
                  sampled_rows=profile['sampled_rows'])
    if explicit and len(explicit) != profile['total_columns']:
        return {**report, 'status': 'mismatch', 'reason': 'Explicit output contract column count differs from SQL'}
    try:
        inferred = infer_contract(sql, schema)
    except Exception as error:
        # Unsupported SQL/parser dependency cannot silently become a passing check.
        inferred = []
        report['analysis_note'] = f'{type(error).__name__}: {error}'[:250]
    mismatch, uncertain = False, not profile['complete']
    for item in profile['columns']:
        index = item['index']
        contract = inferred[index] if index < len(inferred) else {'index': index, 'expected': 'unknown', 'sources': []}
        expected = explicit[index] if explicit else contract['expected']
        # A supplied numeric contract never authorizes erasing known text identifiers.
        conflict = (bool(explicit) and contract.get('protected_identifier') and
                    contract['expected']=='text' and expected in ('integer','number'))
        observed = set(item['storage_classes']) - {'null'}
        allowed = {'integer'} if expected=='integer' else {'integer','real'} if expected=='number' else {'text'}
        status = 'unverified' if expected=='unknown' or not observed else 'ok'
        if conflict or contract.get('unsafe_identifier_cast') or (expected!='unknown' and bool(observed - allowed)):
            status = 'mismatch'
        mismatch |= status=='mismatch'
        uncertain |= status=='unverified'
        report['columns'].append({**contract, **item, 'expected': expected, 'status': status,
                                  'contract_conflict': bool(conflict)})
    if len(profile['columns']) != profile['total_columns']:
        uncertain = True
    report['status'] = 'mismatch' if mismatch else 'unverified' if uncertain else 'ok'
    if mismatch:
        report['guidance'] = ('Correct the final SELECT projection, not only predicates. Preserve text identifiers, '
                              'postal codes, phones and leading zeroes. Do not cast malformed numbers to zero '
                              'or truncate decimals; do not alter NULLs or coerce all numeric-looking text.')
    return report
