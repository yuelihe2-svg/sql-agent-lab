"""Stage a pinned, self-contained BIRD seed into an owned node-local directory.

Never open the shared database through SQLite or copy shared WAL/SHM state.
A nonempty source WAL is refused: first prepare a complete, pinned snapshot.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

LOCAL_FILESYSTEMS = {'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'tmpfs', 'overlay'}


def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def filesystem_type(path):
    result = subprocess.run(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(path)],
                            text=True, capture_output=True, check=True)
    return result.stdout.strip()


def choose_local_base(environ=None):
    env = os.environ if environ is None else environ
    # The explicit override must be valid; never silently fall back from it.
    candidates = ([env['MACSQL_LOCAL_TMPDIR']] if env.get('MACSQL_LOCAL_TMPDIR') else
                  [env.get('SLURM_TMPDIR'), env.get('TMPDIR'), '/tmp'])
    checked = []
    for name in dict.fromkeys(p for p in candidates if p):
        path = Path(name).resolve()
        if not path.is_dir():
            checked.append(f'{path}: absent'); continue
        kind = filesystem_type(path)
        if kind in LOCAL_FILESYSTEMS:
            return path, kind
        checked.append(f'{path}: {kind}')
    raise ValueError('No verified node-local filesystem: ' + '; '.join(checked))


def _no_live_wal(path):
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Nonempty source WAL: stage a complete checkpointed/pinned snapshot first')


def stage_question(data_dir, question_id, local_base, *, require_local=True):
    start = time.monotonic()
    data, base = Path(data_dir).resolve(), Path(local_base).resolve(strict=True)
    kind = filesystem_type(base) if require_local else 'test-fixture'
    if require_local and kind not in LOCAL_FILESYSTEMS:
        raise ValueError(f'Refusing shared or unknown filesystem {kind!r}: {base}')
    lock_bytes = (data/'source-lock.json').read_bytes()
    annotations = (data/'mini_dev_sqlite.jsonl').read_bytes()
    lock = json.loads(lock_bytes)
    if hashlib.sha256(annotations).hexdigest() != lock['annotations_sha256']:
        raise ValueError('Annotation checksum mismatch')
    matches = [row for row in (json.loads(line) for line in annotations.splitlines() if line.strip())
               if str(row['question_id']) == str(question_id)]
    if len(matches) != 1:
        raise ValueError('Expected one requested question')
    db = matches[0]['db_id']
    if not re.fullmatch(r'[A-Za-z0-9_]+', db):
        raise ValueError('Invalid database identifier')
    entry = lock['databases'][db]
    relative = Path(entry['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Database path must be a relative dataset path')
    source = (data/relative).resolve(strict=True)
    _no_live_wal(source)
    size = source.stat().st_size
    if shutil.disk_usage(base).free < size + len(annotations) + 16*1024*1024:
        raise OSError('Insufficient space for a private database copy')
    root = Path(tempfile.mkdtemp(prefix='agentopt-db-', dir=base)).resolve()
    assert root.parent == base and root.name.startswith('agentopt-db-')
    token = uuid4().hex
    try:
        target = root/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        _no_live_wal(source)
        copied_hash = sha256(target)
        if copied_hash != entry['sha256']:
            raise ValueError('Staged database checksum mismatch')
        if target.samefile(source):
            raise ValueError('Staging must create an independent file')
        (root/'source-lock.json').write_bytes(lock_bytes)
        (root/'mini_dev_sqlite.jsonl').write_bytes(annotations)
        marker = {'token': token, 'base': str(base), 'root': str(root)}
        (root/'.agentopt-owned.json').write_text(json.dumps(marker), encoding='utf8')
        return {**marker, 'data_dir': str(root), 'database_path': str(target), 'source_database': str(source),
                'db_id': db, 'question_id': str(question_id), 'sha256': copied_hash,
                'bytes': size, 'filesystem': kind, 'copied_sidecars': False,
                'elapsed_seconds': time.monotonic()-start}
    except BaseException:
        # Only the exact fresh child created above is removed; never source/base.
        if root.parent == base and root.name.startswith('agentopt-db-') and not root.is_symlink():
            shutil.rmtree(root)
        raise


def cleanup_staged(report):
    root, base = Path(report['root']), Path(report['base']).resolve(strict=True)
    if not root.exists():
        return False
    if root.is_symlink() or root.resolve() != root or root.parent != base or not root.name.startswith('agentopt-db-'):
        raise ValueError('Refusing cleanup outside the owned staging directory')
    marker = json.loads((root/'.agentopt-owned.json').read_text(encoding='utf8'))
    if marker != {k: report[k] for k in ('token', 'base', 'root')}:
        raise ValueError('Staging ownership token mismatch')
    shutil.rmtree(root)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['stage', 'cleanup'])
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--question-id')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'cleanup':
        report = json.loads(args.report.read_text(encoding='utf8'))
        report['cleaned_up'] = cleanup_staged(report)
        args.report.write_text(json.dumps(report, indent=2), encoding='utf8')
        return
    if args.data_dir is None or args.question_id is None:
        parser.error('stage requires --data-dir and --question-id')
    if args.report.exists():
        raise FileExistsError('Do not overwrite an existing staging report')
    base, _ = choose_local_base()
    report = stage_question(args.data_dir, args.question_id, base)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x', encoding='utf8') as f:
            json.dump(report, f, indent=2)
    except BaseException:
        cleanup_staged(report)
        raise
    print(report['data_dir'])


if __name__ == '__main__':
    main()
