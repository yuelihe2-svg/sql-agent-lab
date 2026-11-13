"""Prioritize actual SQL sources without treating a partial schema as complete."""
import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


def select_review_schema(schema, sql):
    lookup = {name.casefold(): name for name in schema}
    reason = 'candidate_tables_and_direct_fk_targets'
    try:
        tree = sqlglot.parse_one(sql, read='sqlite')
        scopes = list(traverse_scope(tree))
        referenced = {source.name.casefold() for scope in scopes
                      for source in scope.sources.values() if isinstance(source, exp.Table)}
        if not referenced or not referenced <= lookup.keys():
            raise ValueError('Cannot resolve every physical table in candidate')
        selected = {lookup[name] for name in referenced}
        for name in list(selected):
            for fk in schema[name]['foreign_keys']:
                if str(fk.get('table', '')).casefold() in lookup:
                    selected.add(lookup[fk['table'].casefold()])
    except (ValueError, sqlglot.errors.SqlglotError):
        selected = set(schema)
        reason = 'full_schema_fallback_unresolved_sql_sources'
    included = [name for name in schema if name in selected]
    omitted = [name for name in schema if name not in selected]
    # Keep every column and relation of selected tables. The model receives the
    # omitted table inventory so it cannot mistake this for a complete schema.
    return {name: schema[name] for name in included}, {
        'reason': reason, 'included_tables': included, 'omitted_tables': omitted,
        'all_columns_of_included_tables_preserved': True,
        'types_required': True, 'schema_complete': not omitted,
        'note': 'Unshown table columns are unknown, not absent. Flag missing schema evidence; do not invent it.'}
