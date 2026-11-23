"""SQLite tools: trusted database binding, bounded previews, full-set scoring."""

from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import math
import sqlite3
import time


class EvaluationLimitExceeded(RuntimeError):
    """Full scoring cannot be completed under the configured resource limit."""


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def user_tables(conn):
    listed = conn.execute('PRAGMA main.table_list')
    if listed.description:
        return sorted(row[1] for row in listed if row[2] in ('table', 'virtual')
                      and not row[1].startswith('sqlite_'))
    # Older SQLite builds do not expose the table/shadow distinction.
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name")]


def visible_columns(conn, table):
    # table_xinfo includes generated columns. Flag 1 marks virtual-table internals.
    return [row for row in conn.execute(f'PRAGMA table_xinfo({quote_identifier(table)})') if row[6] != 1]


@dataclass(frozen=True)
class ToolLimits:
    timeout_seconds: float = 2.0
    preview_rows: int = 5
    preview_columns: int = 12
    cell_chars: int = 160
    error_chars: int = 500
    max_sql_chars: int = 50_000
    max_result_rows: int = 100_000
    max_result_bytes: int = 32_000_000
    max_value_bytes: int = 2_000_000
    sample_scan_rows: int = 20
    sample_values: int = 2
    metadata_chars: int = 400
    type_sample_rows: int = 1000

    def __post_init__(self):
        if any(value <= 0 for value in vars(self).values()):
            raise ValueError("Tool limits must be positive")


class SQLiteTools:
    def __init__(self, databases: dict[str, Path], limits=ToolLimits(), *, metadata=None, cancelled=None):
        self.databases = {key: Path(path).resolve(strict=True) for key, path in databases.items()}
        if not self.databases or any(not path.is_file() for path in self.databases.values()):
            raise ValueError("Bind existing database files before running the workflow")
        self.limits = limits
        self.metadata = metadata or {}
        self.cancelled = cancelled

    @contextmanager
    def connection(self, db_id, *, query=False, strict_identifiers=False,
                   timeout_seconds=None):
        # db_id is an allowlisted key, never a model-supplied path or URI.
        path = self.databases[db_id]
        timeout = self.limits.timeout_seconds if timeout_seconds is None else min(
            self.limits.timeout_seconds, timeout_seconds)
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=timeout)
        try:
            if strict_identifiers:
                # Fail explicitly on unsupported Python/SQLite; no silent lax fallback.
                if not hasattr(conn, 'setconfig') or not hasattr(sqlite3, 'SQLITE_DBCONFIG_DQS_DML'):
                    raise RuntimeError('Strict SQL validation requires SQLite DQS configuration (Python 3.12+)')
                conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            conn.execute("PRAGMA query_only=ON")
            conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, self.limits.max_value_bytes)
            conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, self.limits.max_sql_chars)
            deadline = time.monotonic() + timeout
            def should_interrupt():
                return time.monotonic() > deadline or (self.cancelled is not None and self.cancelled())
            conn.set_progress_handler(should_interrupt, 100)
            if query:
                allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                           sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

                def authorize(action, arg1, arg2, database, trigger):
                    if action not in allowed:
                        return sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {
                        "load_extension", "readfile", "writefile"
                    }:
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(authorize)
            yield conn
        finally:
            conn.close()

    def load_schema(self, db_id, *, enrich=False, sample_values=True):
        with self.connection(db_id) as conn:
            schema = {}
            for name in user_tables(conn):
                columns = visible_columns(conn, name)
                fks = conn.execute(f"PRAGMA foreign_key_list({quote_identifier(name)})").fetchall()
                schema[name] = {
                    "columns": {c[1]: {"type": c[2], "pk": bool(c[5])} for c in columns},
                    "foreign_keys": [{"column": f[3], "table": f[2], "target": f[4]} for f in fks],
                }
            if enrich:
                self._enrich(conn, db_id, schema, sample_values=sample_values)
            return schema

    def _enrich(self, conn, db_id, schema, *, sample_values=True):
        """Sample a bounded prefix once per table, never DISTINCT-scan whole data."""
        docs = self.metadata.get(db_id, {})
        for table, definition in schema.items():
            columns = list(definition["columns"])
            # Bound each SQLite value before transfer into Python. Prefix samples
            # are explicitly incomplete and must not be treated as enumerations.
            projections = ",".join(
                f"CASE WHEN typeof({ident}) = 'blob' THEN '[BLOB]' "
                f"ELSE substr(CAST({ident} AS TEXT),1,{self.limits.cell_chars + 1}) END"
                for ident in map(quote_identifier, columns))
            rows = (conn.execute(f"SELECT {projections} FROM {quote_identifier(table)} "
                                f"LIMIT {self.limits.sample_scan_rows}").fetchall() if sample_values else [])
            for index, column in enumerate(columns):
                info = definition["columns"][column]
                doc = docs.get(table, {}).get(column, {})
                if doc and not isinstance(doc.get("source"), str):
                    raise ValueError("Column documentation requires a source")
                for key in ("description", "documented_examples"):
                    if doc.get(key):
                        value = str(doc[key])
                        info[key] = value[:self.limits.metadata_chars]
                        if len(value) > self.limits.metadata_chars:
                            info["documentation_truncated"] = True
                if doc:
                    info["documentation_source"] = doc["source"]
                if "enum_meanings" in doc:
                    meanings = doc["enum_meanings"]
                    if not isinstance(meanings, dict):
                        raise ValueError("enum_meanings must be a mapping with a documented source")
                    if (len(meanings) > 20 or any(len(str(k)) + len(str(v)) >
                                               self.limits.metadata_chars for k, v in meanings.items())):
                        raise ValueError("Enum documentation exceeds limits")
                    info["enum_meanings"] = dict(meanings)
                if not sample_values:
                    continue
                values = list(dict.fromkeys(row[index] for row in rows))[:self.limits.sample_values]
                info["observed_samples"] = [v[:self.limits.cell_chars] if isinstance(v, str) else v
                                            for v in values]
                info["samples_incomplete"] = True
                if any(isinstance(v, str) and len(v) > self.limits.cell_chars for v in values):
                    info["sample_cells_truncated"] = True

    def search_schema(self, db_id, question, *, top_k=8, schema=None):
        """Rank exact schema identifiers using a temporary FTS5 metadata index.

        The index contains documentation and bounded samples, never gold SQL.
        Results are hints; callers must keep validating identifiers against the
        full SQLite schema before using them.
        """
        if not isinstance(question, str) or not question.strip():
            raise ValueError('Schema search requires a nonempty question')
        if not isinstance(top_k, int) or not 1 <= top_k <= 12:
            raise ValueError('top_k must be between 1 and 12')
        schema = self.load_schema(db_id, enrich=True) if schema is None else schema
        terms = list(dict.fromkeys(re.findall(r'[^\W_]+', question.casefold())))
        stop = {'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for', 'from',
                'how', 'in', 'is', 'of', 'on', 'or', 'the', 'to', 'what', 'which',
                'who', 'with', 'would', 'please', 'list', 'show', 'find', 'give'}
        terms = [term for term in terms if len(term) >= 2 and term not in stop][:24]
        if not terms:
            return []
        try:
            with closing(sqlite3.connect(':memory:')) as index:
                index.execute('CREATE VIRTUAL TABLE docs USING fts5('
                              'table_name UNINDEXED, column_name UNINDEXED, '
                              'names, description, examples)')
                for table, definition in schema.items():
                    for column, info in definition['columns'].items():
                        split_names = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2',
                                             table + ' ' + column).replace('_', ' ')
                        examples = ' '.join(str(item) for item in info.get('observed_samples', []))
                        documented = str(info.get('documented_examples', ''))
                        enums = ' '.join(f'{k} {v}' for k, v in info.get('enum_meanings', {}).items())
                        index.execute('INSERT INTO docs VALUES (?, ?, ?, ?, ?)',
                                      (table, column, split_names,
                                       str(info.get('description', '')),
                                       (examples + ' ' + documented + ' ' + enums)[:500]))
                expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
                ranked = index.execute(
                    'SELECT table_name, column_name, bm25(docs, 0, 0, 5, 2, 0.5) '
                    'FROM docs WHERE docs MATCH ? ORDER BY bm25(docs, 0, 0, 5, 2, 0.5) LIMIT ?',
                    (expression, top_k)).fetchall()
        except sqlite3.OperationalError as error:
            raise RuntimeError('FTS5 schema search is unavailable') from error
        results = []
        for table, column, rank in ranked:
            info = schema[table]['columns'][column]
            results.append({'table': table, 'column': column, 'type': info.get('type', ''),
                            'description': str(info.get('description', ''))[:120],
                            'source': info.get('documentation_source', 'sqlite_schema'),
                            'samples': info.get('observed_samples', [])[:2],
                            'samples_incomplete': bool(info.get('samples_incomplete')),
                            'rank': round(float(rank), 5)})
        return results

    def lookup_values(self, db_id, table, column, needle, *, limit=3):
        """Return bounded, read-only examples matching a literal question value."""
        if not isinstance(needle, str) or not 1 <= len(needle) <= 80:
            raise ValueError('Value lookup needle must contain 1 to 80 characters')
        if not isinstance(limit, int) or not 1 <= limit <= 5:
            raise ValueError('Value lookup limit must be between 1 and 5')
        schema = self.load_schema(db_id)
        if table not in schema or column not in schema[table]['columns']:
            raise ValueError('Value lookup requires exact existing table and column names')
        escaped = needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        sql = (f'SELECT substr(CAST({quote_identifier(column)} AS TEXT), 1, ?) '
               f'FROM {quote_identifier(table)} WHERE {quote_identifier(column)} '
               "LIKE ? ESCAPE '\\' AND " + quote_identifier(column) + ' IS NOT NULL LIMIT ?')
        try:
            with self.connection(db_id, query=True, timeout_seconds=2) as conn:
                rows = conn.execute(sql, (self.limits.cell_chars + 1,
                                          '%' + escaped + '%', limit * 8 + 1)).fetchall()
        except sqlite3.Error as error:
            return {'status': 'error', 'table': table, 'column': column,
                    'error_type': type(error).__name__,
                    'message': str(error)[:self.limits.error_chars]}
        values = list(dict.fromkeys(row[0][:self.limits.cell_chars] for row in rows))
        return {'status': 'ok', 'table': table, 'column': column,
                'values': values[:limit], 'truncated': len(rows) > limit or
                any(len(row[0]) > self.limits.cell_chars for row in rows),
                'sample_only': True, 'source': 'sqlite_database'}


    def _execute(self, conn, sql):
        if not isinstance(sql, str) or not sql.strip() or len(sql) > self.limits.max_sql_chars:
            raise ValueError("SQL must be nonempty and within the SQL length limit")
        cursor = conn.execute(sql)  # Exactly one statement; no executescript.
        if cursor.description is None:
            raise ValueError("The query must return rows")
        return cursor

    def compile_sql_readonly(self, db_id, sql):
        """Prepare with SQLite's name resolver; never execute the candidate query.

        Do not parse EXPLAIN opcodes or guess aliases with regex. The same read-only
        authorizer applies at prepare time; quoted missing names cannot become strings.
        """
        try:
            if not isinstance(sql, str) or not sql.strip() or len(sql) > self.limits.max_sql_chars:
                raise ValueError('SQL must be nonempty and within the SQL length limit')
            with self.connection(db_id, query=True, strict_identifiers=True) as conn:
                conn.execute('EXPLAIN ' + sql).close()
            return {'status': 'ok', 'phase': 'compile', 'strict_identifiers': True}
        except (sqlite3.Error, ValueError) as error:
            message = str(error)
            result = {'status': 'error', 'phase': 'compile', 'strict_identifiers': True,
                      'error_type': type(error).__name__, 'message': message[:self.limits.error_chars],
                      'truncated': len(message) > self.limits.error_chars}
            lower = message.lower()
            for prefix, kind in [('no such column:', 'missing_column'),
                                 ('no such table:', 'missing_table'),
                                 ('ambiguous column name:', 'ambiguous_column')]:
                if lower.startswith(prefix):
                    name = message[len(prefix):].strip()
                    result['identifier_kind'] = kind
                    result['identifier'] = name[:160]
                    if kind != 'missing_table':
                        schema = self.load_schema(db_id)
                        # Exact names only, including SQLite's case-insensitive names.
                        full = [(t, c) for t, info in schema.items() for c in info['columns']
                                if c.casefold() == name.casefold()]
                        matches = full or [(t, c) for t, info in schema.items() for c in info['columns']
                                           if c.casefold() == name.rsplit('.', 1)[-1].casefold()]
                        result['exact_column_owners'] = [
                            (quote_identifier(t) + '.' + quote_identifier(c))[:240]
                            for t, c in matches[:4]]
                        result['owners_truncated'] = len(matches) > 4
                    break
            return result

    def execute_sql_readonly(self, db_id, sql):
        limits = self.limits
        try:
            with self.connection(db_id, query=True) as conn:
                cursor = self._execute(conn, sql)
                columns = [c[0] for c in cursor.description]
                from .output_types import NUMERIC_TEXT, storage_class
                rows, sampled, sample_bytes, complete = [], 0, 0, True
                profiles = [{'index': i, 'name': name, 'storage_classes': {},
                             'numeric_text_count': 0, 'other_text_count': 0}
                            for i, name in enumerate(columns)]
                for row in cursor:
                    if sampled >= limits.type_sample_rows:
                        complete = False
                        break
                    size = sum(len(v) if isinstance(v, bytes) else len(str(v).encode('utf8')) for v in row)
                    if sample_bytes + size > limits.max_result_bytes:
                        complete = False
                        break
                    sample_bytes += size
                    sampled += 1
                    if len(rows) <= limits.preview_rows:
                        rows.append(row)
                    for item, value in zip(profiles, row):
                        kind = storage_class(value)
                        item['storage_classes'][kind] = item['storage_classes'].get(kind, 0) + 1
                        if kind == 'text':
                            key = 'numeric_text_count' if NUMERIC_TEXT.fullmatch(value.strip()) else 'other_text_count'
                            item[key] += 1
                has_more = len(rows) > limits.preview_rows or not complete
                truncated = has_more or len(columns) > limits.preview_columns

                def short(value):
                    nonlocal truncated
                    if isinstance(value, bytes):
                        value = '0x' + value[:limits.cell_chars].hex()
                    if isinstance(value, float) and not math.isfinite(value):
                        return str(value)
                    if isinstance(value, str) and len(value) > limits.cell_chars:
                        truncated = True
                        return value[:limits.cell_chars] + "…"
                    return value

                preview = [[short(v) for v in row[:limits.preview_columns]]
                           for row in rows[:limits.preview_rows]]
                return {"status": "ok", "columns": [short(c) for c in columns[:limits.preview_columns]],
                        "column_count": len(columns),
                        "preview_rows": preview, "preview_row_count": len(preview),
                        "has_more": has_more, "truncated": truncated,
                        "output_type_profile": {'columns': profiles, 'sampled_rows': sampled,
                                                'total_columns': len(columns), 'complete': complete}}
        except (sqlite3.Error, ValueError) as error:
            text = str(error)
            return {"status": "error", "error_type": type(error).__name__,
                    "message": text[:limits.error_chars], "truncated": len(text) > limits.error_chars}

    def diagnose_sql_readonly(self, db_id, sql, *, max_probes=3, timeout_seconds=2.0):
        from .sql_diagnostics import diagnose
        return diagnose(self, db_id, sql, max_probes=max_probes, timeout_seconds=timeout_seconds)

    def full_result_set(self, db_id, sql):
        return self.full_result_details(db_id, sql)['rows']

    def full_result_details(self, db_id, sql):
        """Bounded complete evaluation result; never a preview or model input."""
        with self.connection(db_id, query=True) as conn:
            cursor = self._execute(conn, sql)
            columns = [item[0] for item in cursor.description or []]
            rows = set()
            result_bytes = 0; count = 0
            for count, row in enumerate(cursor, 1):
                if count > self.limits.max_result_rows:
                    raise EvaluationLimitExceeded("Scoring row limit exceeded; no preview-based score is valid")
                result_bytes += sum(len(v) if isinstance(v, bytes) else len(str(v).encode("utf-8"))
                                    for v in row)
                if result_bytes > self.limits.max_result_bytes:
                    raise EvaluationLimitExceeded("Scoring byte limit exceeded; no preview-based score is valid")
                rows.add(row)
            return {'rows': rows, 'columns': columns, 'row_count': count,
                    'unique_row_count': len(rows), 'column_count': len(columns)}


def validate_schema_selection(schema, selection):
    """Reject unknown identifiers; retain primary keys and selected-table FK keys."""
    if not isinstance(selection, dict) or not selection:
        raise ValueError("Selector must return a nonempty columns mapping")
    wanted = {}
    for table, columns in selection.items():
        if table not in schema or not isinstance(columns, list) or not columns:
            raise ValueError(f"Selector returned an unknown table or empty column selection: {table!r}")
        for column in columns:
            if not isinstance(column, str) or column not in schema[table]["columns"]:
                owners = [name for name, info in schema.items()
                          if isinstance(column, str) and column in info["columns"]]
                hint = (f" This exact column exists in tables: {owners}; include its actual table and join keys."
                        if owners else " No column with this name exists in the supplied schema; use documented names.")
                raise ValueError(f"Selector returned an unknown column: {table}.{column}.{hint}")
        wanted[table] = set(columns)
        wanted[table].update(c for c, info in schema[table]["columns"].items() if info["pk"])
    for table in wanted:
        for fk in schema[table]["foreign_keys"]:
            if fk["table"] in wanted:
                wanted[table].add(fk["column"])
                target = fk["target"]
                if target:  # Implicit parent primary keys are already retained.
                    wanted[fk["table"]].add(target)
    return {table: {"columns": {c: info for c, info in schema[table]["columns"].items() if c in columns},
                    "foreign_keys": [fk for fk in schema[table]["foreign_keys"] if fk["table"] in wanted]}
            for table, columns in wanted.items()}
