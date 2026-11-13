"""Restore schema evidence after a SQLite missing-name error, without guessing."""
import sqlglot
from sqlglot import exp


def recovery_schema(full, selected, sql, feedback):
    missing = feedback.get('identifier', '')
    tail = missing.rsplit('.', 1)[-1].strip('"`[]').casefold()
    exact_names = {tail, missing.strip('"`[]').casefold()}
    tables = set(selected) & set(full)
    owners = []
    try:
        referenced = {t.name.casefold() for t in sqlglot.parse_one(sql, read='sqlite').find_all(exp.Table)}
    except sqlglot.errors.SqlglotError:
        referenced = set()
    for name, info in full.items():
        if name.casefold() in referenced:
            tables.add(name)
        if feedback.get('identifier_kind') == 'missing_table':
            match = name.casefold() in exact_names
        else:
            match = any(c.casefold() in exact_names for c in info['columns'])
        if match:
            owners.append(name)
            tables.add(name)
    # Keep all exact matches (ambiguity is for the repair model to resolve),
    # plus direct FK targets. Never fabricate a missing name or choose a fuzzy one.
    for name in list(tables):
        for fk in full[name]['foreign_keys']:
            tables.update(t for t in full if t.casefold() == str(fk['table']).casefold())
    return {t: full[t] for t in full if t in tables}, {
        'identifier_kind': feedback.get('identifier_kind'), 'identifier': missing,
        'exact_match_tables': owners, 'identifier_found_in_database': bool(owners),
        'included_tables': [t for t in full if t in tables],
        'omitted_tables': [t for t in full if t not in tables],
        'all_columns_of_included_tables_preserved': True,
        'note': 'A physical column may still be missing from a CTE/subquery projection. No match does not authorize inventing a column.'}
