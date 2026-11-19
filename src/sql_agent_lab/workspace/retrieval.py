"""Schema and knowledge retrieval.

`keyword` is the deterministic baseline and the default. `bm25` keeps that
schema match and ranks user knowledge with FTS5 BM25. `hybrid` adds declared
foreign keys that touch one matched table, still without adding the two scores
together. An embedding strategy can be registered later; none is loaded here.
"""
import re

from sql_agent_lab.workspace.knowledge_index import (
    FTS5Unavailable, KnowledgeIndexError, MemoryKnowledgeIndex,
)


QUESTION_LIMIT = 1000
MAX_TABLES = 8
MAX_COLUMNS = 8
MAX_KNOWLEDGE = 5
EXCERPT_CHARS = 400
_WEAK = frozenset({
    'a', 'an', 'the', 'of', 'and', 'or', 'to', 'in', 'for', 'on', 'by', 'with',
    'how', 'many', 'what', 'which', 'show', 'list', 'from', 'each', 'all',
    'is', 'are', 'me', 'please', 'id', 'no', 'key', 'type', 'date',
    '的', '了', '是', '什么', '哪些', '多少', '和', '与',
})
_INSTRUCTION = re.compile(
    r'ignore\s+(all\s+)?previous|system\s+prompt|drop\s+table|you\s+are\s+now',
    re.IGNORECASE)
_CACHE = {}
STRATEGIES = ('keyword', 'bm25', 'hybrid')
_EXTRA_KNOWLEDGE_STRATEGIES = {}


def register_knowledge_strategy(name, search):
    """Register a future knowledge channel. Built-in names stay fixed.

    `search(documents, datasource_id, question, limit)` returns knowledge hits.
    Calling this does not download a model.
    """
    if name in STRATEGIES or name == 'embedding':
        raise ValueError('A built-in retrieval strategy cannot be replaced')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('strategy name is required')
    _EXTRA_KNOWLEDGE_STRATEGIES[name] = search


def ground(datasource_id, profile, documents, question, cache=None, strategy='keyword',
           knowledge_index=None):
    """Return a bounded grounding bundle for one data source."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError('question is required')
    if len(question) > QUESTION_LIMIT:
        raise ValueError('question is too long')
    if not isinstance(profile, dict) or not isinstance(profile.get('fingerprint'), str):
        raise ValueError('A database profile is required')
    strategy = _resolve_strategy(strategy)
    key = (datasource_id, profile['fingerprint'], _knowledge_key(documents, datasource_id),
           ' '.join(question.lower().split()), strategy)
    store = _CACHE if cache is None else cache
    cached = store.get(key)
    if cached is not None:
        result = dict(cached)
        result['cache'] = 'hit'
        return result
    tokens = _tokens(question)
    tables = _tables(profile, tokens, question)
    selected = {table['name'] for table in tables}
    relationships = _relationships(profile, selected, one_sided=strategy == 'hybrid')
    knowledge, index_status = _knowledge_channel(
        strategy, documents, tokens, datasource_id, profile, question, knowledge_index)
    result = {
        'datasource_id': datasource_id,
        'fingerprint': profile['fingerprint'],
        'question': question.strip(),
        'strategy': strategy,
        'channels': {
            'schema': 'keyword',
            'relationships': 'declared_foreign_key',
            'knowledge': 'keyword' if strategy == 'keyword' else 'bm25' if strategy in ('bm25', 'hybrid') else strategy,
        },
        'score_channels_combined': False,
        'embedding_configured': False,
        'tables': tables,
        'relationships': relationships,
        'knowledge': knowledge,
        'index_status': index_status,
        'schema_facts': 'catalog',
        'business_definitions': 'user_provided_unverified',
        'system_rules': 'not_overridable',
        'cache': 'miss',
        'limits': {
            'tables': MAX_TABLES,
            'columns': MAX_COLUMNS,
            'knowledge': MAX_KNOWLEDGE,
            'excerpt_chars': EXCERPT_CHARS,
        },
    }
    store[key] = result
    return result


def _resolve_strategy(strategy):
    if strategy is None or strategy == 'keyword':
        return 'keyword'
    if strategy in ('bm25', 'hybrid') or strategy in _EXTRA_KNOWLEDGE_STRATEGIES:
        return strategy
    if strategy == 'embedding':
        raise ValueError('embedding retrieval is not configured')
    raise ValueError('retrieval_strategy must be keyword, bm25, or hybrid')


def retrieval_scores(predicted, expected):
    """Precision, recall, and hit for one labeled table or column set."""
    found = set(predicted)
    wanted = set(expected)
    overlap = found & wanted
    if not found and not wanted:
        return {'precision': 1.0, 'recall': 1.0, 'hit': True}
    precision = len(overlap) / len(found) if found else 0.0
    recall = len(overlap) / len(wanted) if wanted else 0.0
    return {'precision': precision, 'recall': recall, 'hit': wanted <= found}


def _tables(profile, tokens, question):
    ranked = []
    for table in profile.get('tables') or []:
        if not isinstance(table, dict) or not isinstance(table.get('name'), str):
            continue
        table_score, table_reason = _name_score(table['name'], tokens, question)
        columns = []
        for column in table.get('columns') or []:
            if not isinstance(column, dict) or not isinstance(column.get('name'), str):
                continue
            score, reason = _name_score(column['name'], tokens, question)
            if score:
                columns.append({
                    'name': column['name'],
                    'type': column.get('type') or '',
                    'score': score,
                    'reason': reason,
                    'schema_facts': 'catalog',
                })
        columns.sort(key=lambda item: (-item['score'], item['name']))
        if not table_score and not columns:
            continue
        ranked.append({
            'name': table['name'],
            'score': table_score + sum(item['score'] for item in columns),
            'reason': table_reason or 'a column name in this table appears in the question',
            'schema_facts': 'catalog',
            'columns': columns[:MAX_COLUMNS],
        })
    ranked.sort(key=lambda item: (-item['score'], item['name']))
    return ranked[:MAX_TABLES]


def _relationships(profile, selected, *, one_sided=False):
    found = []
    for item in profile.get('relationships') or []:
        if not isinstance(item, dict) or item.get('origin') != 'declared_foreign_key':
            continue
        both = item.get('from_table') in selected and item.get('to_table') in selected
        touched = item.get('from_table') in selected or item.get('to_table') in selected
        if not both and not (one_sided and touched):
            continue
        found.append({
            'from_table': item['from_table'],
            'from_columns': list(item.get('from_columns') or []),
            'to_table': item['to_table'],
            'to_columns': list(item.get('to_columns') or []),
            'origin': 'declared_foreign_key',
            'attachment': 'both_tables_matched' if both else 'one_matched_table',
            'reason': 'both tables matched and this foreign key is declared' if both
            else 'one matched table has this declared foreign key',
            'schema_facts': 'catalog',
            'score_kind': 'declared_foreign_key',
        })
    return found


def _knowledge_key(documents, datasource_id):
    rows = []
    for document in documents or []:
        if isinstance(document, dict) and document.get('datasource_id') == datasource_id:
            rows.append((str(document.get('id')), document.get('version'), document.get('content_hash')))
    return tuple(sorted(rows))


def _knowledge(documents, tokens, datasource_id, identifiers=()):
    ranked = []
    identifiers = set(identifiers)
    for document in documents or []:
        if not isinstance(document, dict) or document.get('datasource_id') != datasource_id:
            continue
        title = document.get('title') if isinstance(document.get('title'), str) else ''
        body = document.get('body') if isinstance(document.get('body'), str) else ''
        overlap = sorted(
            token for token in tokens
            if token not in _WEAK and len(token) >= 3 and token in _tokens(title + '\n' + body))
        distinctive = [token for token in overlap if token not in identifiers]
        if not distinctive:
            continue
        excerpt = body.strip()
        truncated = len(excerpt) > EXCERPT_CHARS
        if truncated:
            excerpt = excerpt[:EXCERPT_CHARS]
        ranked.append({
            'id': document.get('id'),
            'version': document.get('version'),
            'content_hash': document.get('content_hash'),
            'provenance': 'user',
            'verification': 'user_provided_unverified',
            'title': title,
            'excerpt': excerpt,
            'excerpt_truncated': truncated,
            'score': len(distinctive),
            'score_kind': 'keyword',
            'reason': 'shared keywords ' + ', '.join(distinctive[:6]),
            'treat_as_instruction': False,
            'instruction_like_text': bool(_INSTRUCTION.search(title + '\n' + body)),
        })
    ranked.sort(key=lambda item: (-item['score'], str(item['id'])))
    return ranked[:MAX_KNOWLEDGE]


def _identifier_tokens(profile):
    found = set()
    for table in profile.get('tables') or []:
        if isinstance(table, dict):
            found.update(_tokens(str(table.get('name') or '')))
            for column in table.get('columns') or []:
                if isinstance(column, dict):
                    found.update(_tokens(str(column.get('name') or '')))
    stems = set()
    for token in found:
        stems.add(token)
        if len(token) >= 4 and token.endswith('s'):
            stems.add(token[:-1])
    return {token for token in stems if token not in _WEAK and len(token) >= 3}


def _name_score(name, tokens, question):
    parts = [part.lower() for part in re.split(r'[^A-Za-z0-9]+', name) if part]
    compact = ''.join(parts)
    lowered = name.lower()
    if lowered in tokens or compact in tokens or _word(question, lowered) or _word(question, compact):
        return 5, f'{name} appears in the question'
    hits = []
    for part in parts:
        if part in _WEAK or len(part) < 3:
            continue
        if part in tokens or _word(question, part):
            hits.append(part)
        elif len(parts) == 1 and len(part) >= 4 and part.endswith('s') and (part[:-1] in tokens or _word(question, part[:-1])):
            hits.append(part)
        elif len(parts) == 1 and ((part + 's') in tokens or _word(question, part + 's')):
            hits.append(part)
    if not hits:
        return 0, ''
    return len(hits), name + ' matches ' + ', '.join(hits)


def _word(question, word):
    if len(word) < 3:
        return False
    return re.search(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', question, re.IGNORECASE) is not None


def _tokens(text):
    found = set()
    for match in re.finditer(r'[A-Za-z0-9_]+|[\u4e00-\u9fff]+', text.lower()):
        word = match.group(0)
        found.add(word)
        if '_' in word:
            found.add(word.replace('_', ''))
            found.update(part for part in word.split('_') if part)
        if re.fullmatch(r'[\u4e00-\u9fff]+', word):
            for index in range(len(word) - 1):
                found.add(word[index:index + 2])
    return found


def _knowledge_channel(strategy, documents, tokens, datasource_id, profile, question, knowledge_index):
    if strategy == 'keyword':
        return _knowledge(documents, tokens, datasource_id, _identifier_tokens(profile)), 'not_used'
    if strategy in _EXTRA_KNOWLEDGE_STRATEGIES:
        hits = _EXTRA_KNOWLEDGE_STRATEGIES[strategy](documents, datasource_id, question, MAX_KNOWLEDGE)
        return list(hits)[:MAX_KNOWLEDGE], 'extension'
    try:
        index = knowledge_index or MemoryKnowledgeIndex(documents)
        hits = index.search(datasource_id, question, MAX_KNOWLEDGE)
    except FTS5Unavailable:
        raise
    except KnowledgeIndexError:
        raise
    by_id = {}
    for document in documents or []:
        if isinstance(document, dict) and document.get('datasource_id') == datasource_id:
            by_id[document.get('id')] = document
    ranked = []
    for hit in hits:
        document = by_id.get(hit['knowledge_id'])
        if document is None:
            continue
        if str(document.get('version')) != str(hit['version']):
            continue
        if document.get('content_hash') != hit['content_hash']:
            continue
        title = document.get('title') if isinstance(document.get('title'), str) else ''
        body = document.get('body') if isinstance(document.get('body'), str) else ''
        excerpt = body.strip()
        truncated = len(excerpt) > EXCERPT_CHARS
        if truncated:
            excerpt = excerpt[:EXCERPT_CHARS]
        ranked.append({
            'id': document.get('id'),
            'version': document.get('version'),
            'content_hash': document.get('content_hash'),
            'provenance': 'user',
            'verification': 'user_provided_unverified',
            'title': title,
            'excerpt': excerpt,
            'excerpt_truncated': truncated,
            'score': None,
            'bm25_score': hit['bm25_score'],
            'score_kind': 'bm25',
            'reason': _bm25_reason(question, title, body),
            'treat_as_instruction': False,
            'instruction_like_text': bool(_INSTRUCTION.search(title + '\n' + body)),
        })
    status = getattr(knowledge_index, 'status', None) or getattr(index, 'status', 'ok')
    return ranked, status


def _bm25_reason(question, title, body):
    query = fts_tokens(question)
    title_tokens = _tokens(title)
    if any(token.lower() in title_tokens for token in query):
        return 'fts5 bm25 title match; score is not a schema score'
    if query:
        return 'fts5 bm25 body match; score is not a schema score'
    return 'fts5 bm25 match; score is not a schema score'


def fts_tokens(question):
    return [match.group(0) for match in re.finditer(r'[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}', question)][:32]
