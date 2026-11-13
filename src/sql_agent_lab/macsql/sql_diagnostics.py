"""Bounded, read-only evidence for SQL constructs that can silently misbehave."""
import sqlite3
import time
import sqlglot
from sqlglot import exp
from .tools import quote_identifier


def diagnose(tools, db_id, sql, *, max_probes=3, timeout_seconds=2.0):
    if not isinstance(max_probes, int) or not 1 <= max_probes <= 3 or not 0 < timeout_seconds <= 2:
        raise ValueError('Diagnostics allow at most three probes and two seconds total')
    if not isinstance(sql, str) or not sql.strip() or len(sql) > tools.limits.max_sql_chars:
        raise ValueError('SQL is empty or exceeds the tool limit')
    try:
        statements = sqlglot.parse(sql, read='sqlite')
        if len(statements) != 1 or not isinstance(statements[0], exp.Query):
            raise ValueError('Diagnostics require one read-only query')
        tree = statements[0]
    except (sqlglot.errors.ParseError, ValueError) as error:
        return {'status': 'unverified', 'findings': [], 'reason': str(error)[:200]}
    findings = []
    started = time.monotonic()
    nodes = [n for n in tree.find_all(exp.In) if isinstance(n.parent, exp.Not) and n.args.get('query')]
    for node in nodes[:max_probes]:
        finding = {'id': 't' + str(len(findings) + 1), 'code': 'not_in_null_rhs',
                   'sql_fragment': node.parent.sql(dialect='sqlite'), 'status': 'unknown',
                   'detail': 'NOT IN with a NULL on the right may exclude nonmatching rows. Intent must be checked.'}
        subquery = node.args['query'].this
        # Probe an unchanged standalone RHS, preserving its filters and limits.
        # Correlation/outer CTEs cannot be evaluated in isolation: compilation
        # failure is unknown, never evidence that NULL is absent.
        if isinstance(subquery, exp.Select) and len(subquery.expressions) == 1:
            projection = subquery.expressions[0]
            name = projection.alias_or_name
            remaining = timeout_seconds - (time.monotonic() - started)
            if name and not projection.find(exp.Star) and remaining > 0:
                probe = (f'SELECT EXISTS(SELECT 1 FROM ({subquery.sql(dialect="sqlite")}) AS "_rhs" '
                         f'WHERE "_rhs".{quote_identifier(name)} IS NULL)')
                finding['probe_sql'] = probe
                try:
                    with tools.connection(db_id, query=True, strict_identifiers=True,
                                          timeout_seconds=remaining) as conn:
                        found = bool(conn.execute(probe).fetchone()[0])
                    finding.update(status='observed_null' if found else 'no_null_found',
                                   detail=('The exact standalone NOT IN subquery returns at least one NULL; '
                                           'a nonmatching left value therefore cannot make this predicate TRUE.' if found else
                                           'The exact standalone NOT IN subquery returned no NULL during this completed probe; '
                                           'this is not a proof of the overall query semantics.'))
                except (sqlite3.Error, ValueError) as error:
                    finding['probe_error'] = str(error)[:200]
        findings.append(finding)
    return {'status': 'completed', 'findings': findings, 'truncated': len(nodes) > max_probes,
            'elapsed_seconds': time.monotonic() - started,
            'scope': 'NOT IN RHS NULL probes only; no automatic rewrite; other semantic errors may remain'}
