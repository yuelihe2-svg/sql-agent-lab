"""Conservative, evidence-backed local SQL edits; not a semantic proof system.

The lexer only compares tokens and locates exact fragments. SQLite remains the
syntax/identifier authority. No query, model name, or benchmark ID is special.
"""
import json
import re
from .review_quotes import quote_in_source

TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/)|"
    r"(?P<string>'(?:''|[^'])*')|(?P<quoted>\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\])|"
    r"(?P<word>[^\W\d]\w*)|(?P<other>.)", re.UNICODE)


class RevisionRejected(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


def tokens(sql):
    result = []
    for match in TOKEN.finditer(sql):
        kind, value = match.lastgroup, match.group()
        if kind in ('space', 'comment'):
            continue
        if kind == 'word':
            value = value.casefold()
        # Preserve quoted identifiers and literal content: do not strip spaces
        # or comments inside a quoted value, nor assume case-insensitive data.
        result.append((kind, value, match.start(), match.end()))
    while result and result[-1][1] == ';':
        result.pop()
    return result


def signature(sql):
    return [(kind, value) for kind, value, _, _ in tokens(sql)]


def evidence_context(question, schema, sql):
    """Exclude samples from authority; retain relevant documented definitions.

    Definitions for identifiers in the candidate are retained in full as loaded
    by the bounded schema tool. Truncated documentation is not citable.
    """
    names = {value.strip('"`[]').casefold() for kind, value, _, _ in tokens(sql)
             if kind in ('word', 'quoted')}
    sources = {'question': question.question, 'evidence': question.evidence or ''}
    catalog = []
    clean = {}
    for table, definition in schema.items():
        clean[table] = {'foreign_keys': definition['foreign_keys'], 'columns': {}}
        for column, info in definition['columns'].items():
            clean[table]['columns'][column] = {k: info[k] for k in ('type', 'pk') if k in info}
            if column.casefold() not in names or not info.get('documentation_source'):
                continue
            if info.get('documentation_truncated'):
                continue
            for key in ('description', 'enum_meanings'):
                value = info.get(key)
                if not value:
                    continue
                value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                source = 'd' + str(len(catalog) + 1)
                sources[source] = value
                catalog.append({'source': source, 'table': table, 'column': column,
                                'kind': key, 'text': value})
    # Foreign keys are supplied structured schema facts, not question evidence.
    # Keep dN numbering independent for compatibility with saved documentation.
    fk_count = 0
    for table, definition in schema.items():
        if table.casefold() not in names:
            continue  # Cite relations of tables used by this candidate, not every unrelated table.
        for fk in definition['foreign_keys']:
            column, target_table, target_column = fk.get('column'), fk.get('table'), fk.get('target')
            if not all(isinstance(v, str) and v for v in (column, target_table, target_column)):
                continue  # Missing target columns are not invented as authoritative facts.
            fk_count += 1; source = 'schema_fk_' + str(fk_count)
            canonical = f'FK: {table}.{column} -> {target_table}.{target_column}'
            legacy = f'FK: {column} -> {target_table}.{target_column}'
            value = canonical + '\nSchema display: ' + legacy
            sources[source] = value
            catalog.append({'source': source, 'kind': 'schema_foreign_key', 'table': table,
                'column': column, 'target_table': target_table, 'target_column': target_column, 'text': value})
    return clean, sources, catalog


def inspect_candidate(before, candidate, sources):
    """Reject unchanged token streams and unsupported new string literals."""
    if signature(before) == signature(candidate):
        raise RevisionRejected('no_effect', 'Only formatting/comments changed; no SQL correction was made')
    old_values = {value[1:-1].replace("''", "'") for kind, value, _, _ in tokens(before) if kind == 'string'}
    new_values = {value[1:-1].replace("''", "'") for kind, value, _, _ in tokens(candidate) if kind == 'string'}
    for value in sorted(new_values - old_values):
        if value and not any(value.casefold() in source.casefold() for source in sources.values()):
            raise RevisionRejected('unsupported_literal', 'New literal lacks question/documentation support: ' + value[:120])


def apply_revision(before, report, question, sources):
    """Apply grounded nonoverlapping replacements, never trust a duplicate SQL.

    Each original fragment must have exactly one token match. Insertion/removal
    is expressed by replacing a surrounding clause, avoiding ambiguous anchors.
    """
    changes = report.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 6:
        # Diagnose actual legacy responses without silently accepting their SQL.
        if isinstance(report.get('sql'), str):
            inspect_candidate(before, report['sql'], sources)
        raise RevisionRejected('missing_changes', 'Revision requires 1–6 grounded local replacements')
    failed = {k for k, v in report['checks'].items() if v['status'] == 'fail'}
    if any(v['status'] == 'uncertain' for v in report['checks'].values()):
        raise RevisionRejected('unresolved_checks', 'Unresolved uncertainty prevents automatic rewriting')
    original = tokens(before)
    original_sig = [(k, v) for k, v, _, _ in original]
    patches, covered = [], set()
    requirement = (question.question + '\n' + (question.evidence or '')).casefold()
    for change in changes:
        if not isinstance(change, dict):
            raise RevisionRejected('invalid_change', 'Each change must be an object')
        checks = change.get('checks')
        if (not isinstance(checks, list) or not checks or
                any(not isinstance(k, str) or k not in failed for k in checks)):
            raise RevisionRejected('invalid_check_mapping', 'Each edit must name failed checks only')
        quote = change.get('requirement_quote')
        if not isinstance(quote, str) or len(quote.strip()) < 3 or not quote_in_source(quote.casefold(), requirement):
            raise RevisionRejected('ungrounded_requirement', 'Requirement quote is absent from question/evidence')
        citations = change.get('evidence')
        if not isinstance(citations, list) or not 1 <= len(citations) <= 4:
            raise RevisionRejected('missing_evidence', 'Each edit needs a supplied source and exact quote')
        for citation in citations:
            if not isinstance(citation, dict):
                raise RevisionRejected('invalid_evidence', 'Citation must be an object')
            source, excerpt = citation.get('source'), citation.get('quote')
            if (not isinstance(source, str) or source not in sources or not isinstance(excerpt, str)
                    or len(excerpt.strip()) < 3 or not quote_in_source(excerpt, sources[source])):
                raise RevisionRejected('ungrounded_evidence', 'Citation is not in the supplied source')
        old, new = change.get('before'), change.get('after')
        if not isinstance(old, str) or not isinstance(new, str) or not old.strip() or not new.strip():
            raise RevisionRejected('invalid_fragment', 'Use nonempty surrounding SQL fragments')
        needle = signature(old)
        if not needle or needle == signature(new):
            raise RevisionRejected('no_effect', 'A proposed edit does not change SQL tokens')
        matches = [i for i in range(len(original_sig) - len(needle) + 1)
                   if original_sig[i:i + len(needle)] == needle]
        if len(matches) != 1:
            raise RevisionRejected('ambiguous_fragment', 'Original fragment must match exactly once; include more context')
        start = original[matches[0]][2]
        end = original[matches[0] + len(needle) - 1][3]
        if any(start < b and a < end for a, b, _ in patches):
            raise RevisionRejected('overlapping_edits', 'Edits overlap; combine their failed checks into one edit')
        patches.append((start, end, new))
        covered.update(checks)
    if covered != failed:
        raise RevisionRejected('unaddressed_checks', 'Every failed check must be linked to a replacement')
    candidate = before
    for start, end, replacement in sorted(patches, reverse=True):
        candidate = candidate[:start] + replacement + candidate[end:]
    # Literal checks only use evidence actually cited by these edits.
    cited = {c['source']: sources[c['source']] for change in changes for c in change['evidence']}
    inspect_candidate(before, candidate, cited)
    if 'sql' in report and (not isinstance(report['sql'], str) or signature(report['sql']) != signature(candidate)):
        raise RevisionRejected('conflicting_sql', 'Extra full SQL disagrees with the applied replacements')
    return candidate
