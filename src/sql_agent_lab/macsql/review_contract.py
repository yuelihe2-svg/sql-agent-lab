"""Validate review structure and observable citations, not semantic completeness.

Coverage measures supplied text spans, not whether a model understood them.
No benchmark answers, model names, or question IDs are used here.
"""
import json
import re
from .semantic_guard import RevisionRejected, signature
from .review_quotes import quote_in_source, normalize_quote

CHECKS = ('output', 'filters', 'joins', 'aggregation', 'ordering', 'limit')


class ReviewProtocolError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def contains_fragment(sql, fragment):
    haystack, needle = signature(sql), signature(fragment)
    return bool(needle) and any(haystack[i:i + len(needle)] == needle
                               for i in range(len(haystack) - len(needle) + 1))


def descriptive_anchor(sql, fragment):
    """Allow ordered SQL excerpts separated by ellipses for explanation only.

    Literal strings/identifiers are tokenized intact. Ellipses are never allowed
    as edit locations, and a real operand/operator cannot be invented or reordered.
    """
    haystack, needle = signature(sql), signature(fragment)
    chunks, chunk, abbreviated = [], [], False
    i = 0
    while i < len(needle):
        if needle[i:i+3] == [('other', '.')] * 3 or needle[i] == ('other', '…'):
            if not chunk:
                return False
            chunks.append(chunk); chunk = []; abbreviated = True
            i += 3 if needle[i] == ('other', '.') else 1
        else:
            chunk.append(needle[i]); i += 1
    if not abbreviated or not chunk:
        return False
    chunks.append(chunk)
    if sum(map(len, chunks)) < 3:
        return False
    cursor = 0
    for part in chunks:
        found = next((j for j in range(cursor, len(haystack)-len(part)+1)
                      if haystack[j:j+len(part)] == part), None)
        if found is None:
            return False
        cursor = found + len(part)
    return True


def parse_report(raw):
    text = raw.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.IGNORECASE)
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReviewProtocolError('duplicate_key', 'Duplicate JSON key: ' + key)
            result[key] = value
        return result
    try:
        report = json.loads(text, object_pairs_hook=unique_keys)
    except json.JSONDecodeError as error:
        raise ReviewProtocolError('invalid_json', 'Review must be valid JSON') from error
    if not isinstance(report, dict) or report.get('action') not in ('accept', 'revise', 'uncertain'):
        raise ReviewProtocolError('invalid_action', 'Review must specify accept, revise, or uncertain')
    checks = report.get('checks')
    if isinstance(checks, list):
        normalized = {}
        for item in checks:
            if not isinstance(item, dict):
                raise ReviewProtocolError('invalid_checks', 'Each check must be an object')
            labels = [item[k] for k in ('name', 'check') if k in item]
            if not labels or any(not isinstance(v, str) for v in labels) or len(set(labels)) != 1:
                raise ReviewProtocolError('invalid_check_name', 'Use one unambiguous name/check label')
            name = labels[0]
            if name in normalized:
                raise ReviewProtocolError('duplicate_check', 'Duplicate check: ' + name)
            normalized[name] = {k: v for k, v in item.items() if k not in ('name', 'check')}
        checks = normalized
        report['checks'] = checks
        report['format_normalized'] = 'checks_list_to_mapping'
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ReviewProtocolError('incomplete_checks', 'Review must cover all six checks; missing checks are not passes')
    for name, item in checks.items():
        if (not isinstance(item, dict) or item.get('status') not in ('pass', 'fail', 'uncertain')
                or not isinstance(item.get('reason'), str) or not item['reason'].strip()):
            raise ReviewProtocolError('invalid_check', f'Invalid review item: {name}')
    statuses = [item['status'] for item in checks.values()]
    if report['action'] == 'accept' and any(s != 'pass' for s in statuses):
        raise ReviewProtocolError('decision_conflict', 'An accept decision conflicts with failed or uncertain checks')
    if report['action'] != 'revise' and (report.get('changes') or report.get('sql')):
        raise ReviewProtocolError('decision_conflict', 'Only revise may propose SQL changes')
    if report['action'] == 'revise':
        projection = report.get('projection_only') is True and isinstance(report.get('output_contract'), dict)
        if 'fail' not in statuses or not (projection or report.get('changes') or isinstance(report.get('sql'), str)):
            raise ReviewProtocolError('missing_changes', 'A revision requires a failed check and local changes')
    if report.get('projection_only') and report['action'] != 'revise':
        raise ReviewProtocolError('decision_conflict', 'Projection-only repair requires revise')
    return report


def validate_requirements(report, question, sql, supplied_sources=None):
    """Check cited clauses and anchors; word coverage is not a semantic test.

    Document citations are supplemental reasoning, not additional user demands.
    Actual edits still use the strict, unique anchors in semantic_guard.
    """
    requirements = report.get('requirements')
    if not isinstance(requirements, list) or not 1 <= len(requirements) <= 20:
        raise ReviewProtocolError('missing_requirements', 'Supply 1–20 explicit requirement items')
    sources = {'question': question.question, 'evidence': question.evidence or ''}
    documents = {k: v for k, v in (supplied_sources or {}).items()
                 if re.fullmatch(r'(?:[dt]\d+|schema_fk_\d+)', k) and isinstance(v, str)}
    sources.update(documents)
    warnings, citations, cited_user_sources = [], [], set()
    ids, identities = set(), set()
    for item in requirements:
        if not isinstance(item, dict):
            raise ReviewProtocolError('invalid_requirement', 'Requirement must be an object')
        rid, quote, check = item.get('id'), item.get('quote'), item.get('check')
        source, status = item.get('source', 'question'), item.get('status')
        fragment, reason = item.get('sql_fragment'), item.get('reason')
        if not isinstance(rid, str) or not re.fullmatch(r'r[1-9]\d?', rid) or rid in ids:
            raise ReviewProtocolError('duplicate_requirement', 'Use unique requirement IDs r1, r2, ...')
        if (not isinstance(source, str) or source not in sources or not isinstance(quote, str)
                or not quote.strip()):
            raise ReviewProtocolError('ungrounded_requirement', 'Quote needs a supplied source and nonempty text')
        matched = [source] if quote_in_source(quote, sources[source]) else []
        if not matched and source in ('question', 'evidence'):
            matched = [k for k, text in documents.items() if quote_in_source(quote, text)]
            if matched:
                if any(k.startswith('schema_fk_') for k in matched) and len(matched) != 1:
                    raise ReviewProtocolError('ambiguous_schema_reference', 'Foreign key excerpt matches multiple source tables; cite the full relation and source ID')
                warnings.append({'code': 'schema_source_recovered' if matched[0].startswith('schema_fk_') else 'document_source_recovered', 'requirement': rid,
                                 'claimed_source': source, 'matched_sources': matched})
        if not matched:
            raise ReviewProtocolError('ungrounded_requirement', 'Quote is absent from supplied question/evidence/documents: ' + rid)
        if matched == [source] and quote not in sources[source]:
            warnings.append({'code': 'quote_format_normalized', 'requirement': rid})
        citations.append({'requirement': rid, 'sources': matched,
                          'kind': 'user_requirement' if matched[0] in ('question', 'evidence') else 'supporting_evidence'})
        cited_user_sources.update(k for k in matched if k in ('question', 'evidence'))
        if (not isinstance(check, str) or check not in CHECKS or status not in ('pass', 'fail', 'uncertain')
                or not isinstance(fragment, str) or not isinstance(reason, str) or not reason.strip()):
            raise ReviewProtocolError('invalid_requirement', 'Requirement needs check, status, SQL fragment and reason')
        identity = (tuple(matched), normalize_quote(quote), check)
        if identity in identities:
            raise ReviewProtocolError('duplicate_requirement', 'Repeated requirement item')
        identities.add(identity)
        ids.add(rid)
        if fragment and not contains_fragment(sql, fragment):
            if report['action'] != 'revise' and descriptive_anchor(sql, fragment):
                warnings.append({'code': 'abbreviated_sql_evidence', 'requirement': rid,
                                 'note': 'Ordered excerpts only; not an edit anchor or proof of SQL semantics'})
            else:
                raise ReviewProtocolError('unmatched_sql_anchor', 'SQL fragment is absent from the actual candidate: ' + rid)
        if status == 'pass' and not fragment.strip():
            raise ReviewProtocolError('missing_sql_anchor', 'A passed requirement needs a concrete SQL fragment: ' + rid)
        if status != 'pass' and report['checks'][check]['status'] != status:
            raise ReviewProtocolError('decision_conflict', 'Requirement contradicts its overall check: ' + rid)
        if report['action'] == 'accept' and status != 'pass':
            raise ReviewProtocolError('decision_conflict', 'Accept requires every requirement to pass')
        if report['action'] == 'revise' and status == 'uncertain':
            raise ReviewProtocolError('unresolved_requirements', 'Do not rewrite unresolved requirements')
    if 'question' not in cited_user_sources:
        raise ReviewProtocolError('missing_question_requirement', 'Cite at least one actual question requirement')
    if question.evidence and 'evidence' not in cited_user_sources:
        warnings.append({'code': 'uncited_evidence', 'note': 'Supplied evidence has no requirement citation; completeness remains unchecked'})
    if report['action'] == 'revise' and not any(r['status'] == 'fail' for r in requirements):
        raise ReviewProtocolError('missing_failed_requirement', 'Revise must identify a concrete failed requirement')
    return {'status': 'validated_with_warnings' if warnings else 'validated',
            'count': len(requirements), 'citations': citations, 'warnings': warnings,
            'semantic_completeness': 'not_verified',
            'scope': 'Report structure and cited evidence only; no word-coverage or semantic correctness claim'}


def check_revision_requirements(before, candidate, report):
    requirements = {r['id']: r for r in report['requirements']}
    failed = {rid for rid, r in requirements.items() if r['status'] == 'fail'}
    linked = set()
    for change in report['changes']:
        references = change.get('requirements')
        if not isinstance(references, list) or not references or any(not isinstance(r, str) or r not in failed for r in references):
            raise RevisionRejected('invalid_requirement_mapping', 'Each change must name its failed requirement IDs')
        for rid in references:
            requirement = requirements[rid]
            if requirement['check'] not in change['checks']:
                raise RevisionRejected('invalid_requirement_mapping', 'Edit check disagrees with its requirement')
            anchor = requirement['sql_fragment']
            if anchor and not (contains_fragment(change['before'], anchor) or contains_fragment(anchor, change['before'])):
                raise RevisionRejected('unrelated_edit', 'Edit does not touch the failed SQL anchor: ' + rid)
        linked.update(references)
    if linked != failed:
        raise RevisionRejected('unaddressed_requirements', 'Every failed requirement needs an explicit edit')
    for rid, requirement in requirements.items():
        if requirement['status'] == 'pass' and not contains_fragment(candidate, requirement['sql_fragment']):
            raise RevisionRejected('passing_requirement_changed', 'Edit removed a previously passed SQL anchor: ' + rid)
    # Preserve the result shape during automatic semantic repair. A user must
    # resolve output-contract ambiguity separately; adding explanation columns
    # merely because they appear in sorting is not an automatic correction.
    import sqlglot
    from sqlglot import exp
    try:
        old, new = (sqlglot.parse_one(s, read='sqlite') for s in (before, candidate))
        if isinstance(old, exp.Select) and isinstance(new, exp.Select):
            if len(old.expressions) != len(new.expressions):
                raise RevisionRejected('output_shape_changed', 'Automatic semantic edits must preserve output column count')
    except sqlglot.errors.ParseError as error:
        raise RevisionRejected('invalid_candidate_sql', 'Edited SQL could not be parsed') from error
