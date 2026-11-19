"""FTS5 index for user knowledge titles and bodies.

The index lives in the workspace database. It does not read business-table rows.
SQLite unicode61 keeps a contiguous Chinese run as one token, so this is not
Chinese semantic search. BM25 scores stay on the knowledge channel.
"""
import re
import sqlite3


class KnowledgeIndexError(ValueError):
    """The knowledge index cannot answer this query."""


class FTS5Unavailable(KnowledgeIndexError):
    """This SQLite build cannot create an FTS5 table."""


FTS_TABLES = (
    'knowledge_fts',
    'knowledge_fts_data',
    'knowledge_fts_idx',
    'knowledge_fts_content',
    'knowledge_fts_docsize',
    'knowledge_fts_config',
)
FTS_CREATE_SQL = (
    'CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5('
    'knowledge_id UNINDEXED, datasource_id UNINDEXED, version UNINDEXED, '
    "content_hash UNINDEXED, title, body, tokenize='unicode61')"
)
# Weights follow table order. Unindexed columns are zero. Title outweighs body.
# The raw bm25 value is negative; a smaller value ranks higher. It is not a schema score.
_BM25 = 'bm25(knowledge_fts, 0.0, 0.0, 0.0, 0.0, 10.0, 1.0)'
_TOKEN = re.compile(r'[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}')


def fts5_available(connection=None):
    """Return whether this process can create an FTS5 table."""
    own = connection is None
    conn = sqlite3.connect(':memory:') if own else connection
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(body)")
        conn.execute('DROP TABLE IF EXISTS _fts5_probe')
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        if own:
            conn.close()


def fts_match_query(question):
    """Quote question tokens so MATCH operators in the text cannot change the query."""
    if not isinstance(question, str):
        return None
    tokens = []
    for match in _TOKEN.finditer(question):
        tokens.append('"' + match.group(0).replace('"', '') + '"')
        if len(tokens) == 32:
            break
    if not tokens:
        return None
    return ' OR '.join(tokens)


def upsert_index(connection, record, body):
    connection.execute('DELETE FROM knowledge_fts WHERE knowledge_id = ?', (record['id'],))
    connection.execute(
        'INSERT INTO knowledge_fts(knowledge_id, datasource_id, version, content_hash, title, body) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (record['id'], record['datasource_id'], str(record['version']), record['content_hash'],
         record.get('title') or '', body))


def delete_index(connection, knowledge_id):
    connection.execute('DELETE FROM knowledge_fts WHERE knowledge_id = ?', (knowledge_id,))


def search_index(connection, datasource_id, question, limit):
    """Return BM25 hits for one data source. A lower bm25_score ranks first."""
    query = fts_match_query(question)
    if query is None:
        return []
    try:
        rows = connection.execute(
            'SELECT knowledge_id, version, content_hash, ' + _BM25 + ' AS bm25_score '
            'FROM knowledge_fts WHERE datasource_id = ? AND knowledge_fts MATCH ? '
            'ORDER BY bm25_score, knowledge_id LIMIT ?',
            (datasource_id, query, limit)).fetchall()
    except sqlite3.DatabaseError as error:
        raise KnowledgeIndexError('Knowledge full-text query failed') from error
    return [{
        'knowledge_id': row[0],
        'version': row[1],
        'content_hash': row[2],
        'bm25_score': float(row[3]),
    } for row in rows]


class MemoryKnowledgeIndex:
    """Index the documents passed to one retrieval call. Nothing is written to disk."""

    def __init__(self, documents):
        self.status = 'memory'
        self.connection = sqlite3.connect(':memory:')
        if not fts5_available(self.connection):
            raise FTS5Unavailable('This SQLite build does not provide FTS5')
        self.connection.execute(FTS_CREATE_SQL)
        for document in documents or []:
            if not isinstance(document, dict) or not isinstance(document.get('body'), str):
                continue
            if not document.get('id') or not document.get('datasource_id'):
                continue
            upsert_index(self.connection, document, document['body'])

    def search(self, datasource_id, question, limit):
        return search_index(self.connection, datasource_id, question, limit)


class WorkspaceKnowledgeIndex:
    """Search the workspace FTS table and rebuild it when rows drift from the files."""

    def __init__(self, store, workspace_db):
        self.store = store
        self.workspace_db = workspace_db
        self.status = 'ok'

    def search(self, datasource_id, question, limit):
        self._repair_if_needed()
        try:
            return self.store.search_knowledge(datasource_id, question, limit)
        except sqlite3.DatabaseError:
            self._rebuild()
            return self.store.search_knowledge(datasource_id, question, limit)

    def _repair_if_needed(self):
        try:
            current = self.store.knowledge_index_current()
        except sqlite3.DatabaseError:
            current = False
        if not current:
            self._rebuild()

    def _rebuild(self):
        from sql_agent_lab.workspace.knowledge import reindex_knowledge
        reindex_knowledge(self.store, self.workspace_db)
        self.status = 'rebuilt'
