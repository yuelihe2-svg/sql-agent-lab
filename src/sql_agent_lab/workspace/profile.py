"""Read-only SQLite catalog profile. Business rows are never copied."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3

from sql_agent_lab.macsql.tools import quote_identifier


def extract_profile(database):
    """Return catalog facts for one SQLite file.

    The returned object has no filesystem path. Declared keys are recorded.
    Missing keys stay empty. No business definition is inferred.
    """
    path = Path(database)
    uri = path.resolve().as_uri() + '?mode=ro'
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            tables = [_table(conn, name) for name in names]
    except sqlite3.Error as error:
        raise ValueError('Data source schema could not be read') from error
    relationships = [
        item for table in tables for item in table['foreign_keys']]
    canonical = {'tables': [_fingerprint_table(table) for table in tables],
                 'relationships': relationships}
    missing_keys = [table['name'] for table in tables if not table['primary_key']]
    return {
        'kind': 'sqlite',
        'tables': tables,
        'relationships': relationships,
        'fingerprint': hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest(),
        'coverage': {
            'table_count': len(tables),
            'tables_without_primary_key': missing_keys,
            'declared_foreign_key_count': len(relationships),
            'business_definitions': 'not_inferred',
        },
    }


def public_profile(source, extracted):
    """Allowlisted profile for one registered source."""
    return {
        'datasource_id': source.id,
        'name': source.name,
        'kind': 'sqlite',
        'read_only': True,
        'fingerprint': extracted['fingerprint'],
        'extracted_at': extracted['extracted_at'],
        'tables': extracted['tables'],
        'relationships': extracted['relationships'],
        'coverage': extracted['coverage'],
        'schema_facts': 'catalog',
        'business_definitions': 'not_inferred',
    }


def _table(conn, name):
    info = conn.execute(f'PRAGMA table_info({quote_identifier(name)})').fetchall()
    foreign_keys = conn.execute(f'PRAGMA foreign_key_list({quote_identifier(name)})').fetchall()
    primary_key = [row[1] for row in sorted(info, key=lambda item: item[5]) if row[5]]
    grouped = {}
    for row in foreign_keys:
        grouped.setdefault(row[0], []).append(row)
    keys = []
    for fk_id in sorted(grouped):
        columns = sorted(grouped[fk_id], key=lambda item: item[1])
        keys.append({
            'from_table': name,
            'from_columns': [item[3] for item in columns],
            'to_table': columns[0][2],
            'to_columns': [item[4] for item in columns],
            'origin': 'declared_foreign_key',
        })
    return {
        'name': name,
        'primary_key': primary_key,
        'columns': [
            {'name': row[1], 'type': row[2] or '', 'primary_key': bool(row[5])}
            for row in info
        ],
        'foreign_keys': keys,
    }


def _fingerprint_table(table):
    return {
        'name': table['name'],
        'primary_key': list(table['primary_key']),
        'columns': [
            {'name': column['name'], 'type': column['type'], 'primary_key': column['primary_key']}
            for column in table['columns']
        ],
        'foreign_keys': table['foreign_keys'],
    }
