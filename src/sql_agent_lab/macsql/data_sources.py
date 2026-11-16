"""Explicit local SQLite bindings, independent of questions and evaluation."""
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3


@dataclass(frozen=True)
class SQLiteSource:
    id: str
    name: str
    path: Path

    def public(self):
        # Browser clients choose an ID; filesystem paths stay on the server.
        return {'id': self.id, 'name': self.name, 'kind': 'sqlite', 'read_only': True}


class SourceRegistry:
    def __init__(self):
        self._sources = {}

    def add(self, source):
        if not isinstance(source.id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', source.id):
            raise ValueError('Data source ID must use 1–100 letters, numbers, underscores or hyphens')
        if source.id in self._sources:
            raise ValueError(f'Duplicate data source ID: {source.id}')
        if not isinstance(source.name, str) or not source.name.strip():
            raise ValueError(f'Data source {source.id}: a display name is required')
        path = Path(source.path).resolve()
        if not path.is_file():
            raise ValueError(f'Data source {source.id}: SQLite file does not exist')
        self._sources[source.id] = SQLiteSource(source.id, source.name.strip(), path)

    def load(self, config):
        config = Path(config).resolve(strict=True)
        document = json.loads(config.read_text(encoding='utf-8-sig'))
        if not isinstance(document, dict) or not isinstance(document.get('sources'), list):
            raise ValueError('Source config must contain a sources array')
        for entry in document['sources']:
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path'].strip():
                raise ValueError('Each data source requires an ID and a SQLite file path')
            if entry.get('kind', 'sqlite') != 'sqlite':
                raise ValueError('Only SQLite data sources are supported')
            source_id = entry.get('id')
            path = (config.parent / entry['path']).resolve()
            # mode=ro never creates a missing database; close explicitly on Windows.
            if not path.is_file():
                raise ValueError(f'Data source {source_id}: SQLite file does not exist')
            with path.open('rb') as handle:
                if handle.read(16) != b'SQLite format 3\x00':
                    raise ValueError(f'Data source {source_id}: not a SQLite database')
            try:
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
                    conn.execute('PRAGMA query_only=ON')
                    conn.execute('SELECT name FROM sqlite_schema LIMIT 1').fetchall()
            except sqlite3.Error as error:
                raise ValueError(f'Data source {source_id}: cannot read SQLite schema: {error}') from error
            self.add(SQLiteSource(source_id, entry.get('name', source_id), path))

    @property
    def databases(self):
        return {key: source.path for key, source in self._sources.items()}

    def catalog(self):
        return [self._sources[key].public() for key in sorted(self._sources)]
