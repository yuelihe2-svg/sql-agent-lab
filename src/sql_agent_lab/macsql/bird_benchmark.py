"""Optional pinned BIRD input. Reference SQL stays outside the Agent input."""
import json
from pathlib import Path

from .data_sources import SQLiteSource
from .dataset import load_bird_jsonl
from .fetch_bird import sha256


class BirdBenchmark:
    def __init__(self, directory, registry):
        directory = Path(directory).resolve()
        lock = json.loads((directory / 'source-lock.json').read_text(encoding='utf-8'))
        annotations = directory / 'mini_dev_sqlite.jsonl'
        if sha256(annotations) != lock['annotations_sha256']:
            raise ValueError('Dataset checksum mismatch')
        databases = {}
        self.expected_hashes = {}
        for db_id, entry in lock['databases'].items():
            source = SQLiteSource(db_id, db_id, directory / entry['path'])
            registry.add(source)
            databases[db_id] = source.path
            self.expected_hashes[db_id] = entry['sha256']
        self.dataset = load_bird_jsonl(annotations, databases)

    def question(self, question_id):
        pair = next((p for p in self.dataset if p[0].question_id == str(question_id)), None)
        if pair is None:
            raise ValueError('Unknown benchmark question')
        return pair

    def verify_database(self, db_id, actual_hash):
        if actual_hash != self.expected_hashes[db_id]:
            raise ValueError('Database checksum mismatch')
